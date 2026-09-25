//! The Slint desktop frontend: the manager window and the system-tray icon.
//!
//! Every [`Manager`] call that can block (starting, stopping, restarting)
//! runs on a worker thread while the window shows a busy state; the UI thread
//! only ever `try_lock`s the manager, so it never waits on a slow stop.

use std::cell::{Cell, RefCell};
use std::process::ExitCode;
use std::rc::Rc;
use std::sync::mpsc::Receiver;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError, TryLockError};
use std::time::Duration;

use slint::{
    CloseRequestResponse, ComponentHandle, ModelRc, PlatformError, Timer, TimerMode, VecModel,
};

use crate::frontend::{AppEvent, Frontend};
use crate::manager::{Applied, Manager, ManagerError};
use crate::process::{ServiceSnapshot, ServiceStatus};
use crate::routes::{RouteGroup, ServiceKind};

mod syntax;

mod ui {
    slint::include_modules!();
}

use ui::{
    Level, ManagerTray, ManagerWindow, RouteRow, ServiceRow, ServiceView, TextPosition, TomlSyntax,
    TomlToken, TomlTokenKind,
};

/// Matches the crash monitor's poll interval.
const REFRESH_INTERVAL: Duration = Duration::from_secs(2);
const SERVICES_TAB: i32 = 0;
const LOGS_TAB: i32 = 1;
const ROUTES_TAB: i32 = 2;

const MONO_FONT: &str = if cfg!(target_os = "macos") {
    "Menlo"
} else if cfg!(windows) {
    "Consolas"
} else {
    "DejaVu Sans Mono"
};

/// The manager window plus a tray icon; closing the window hides it while
/// the proxy and services keep running.
#[derive(Debug, Default)]
pub struct Desktop;

impl Frontend for Desktop {
    fn run(self: Box<Self>, manager: Manager, events: Receiver<AppEvent>) -> ExitCode {
        let manager = Arc::new(Mutex::new(manager));
        // AppKit's own quit paths (the app menu's Quit and its Cmd-Q, the
        // Dock's Quit, logging out) exit the process from inside the event
        // loop, so this never regains control after them.
        let _terminate = app_exit::on_terminate({
            let manager = Arc::clone(&manager);
            move || shut_down(&manager)
        });
        let result = run_event_loop(Arc::clone(&manager), events);
        // However the event loop ended, the window is gone; stop everything.
        shut_down(&manager);
        match result {
            Ok(()) => ExitCode::SUCCESS,
            Err(err) => {
                tracing::error!(
                    "Could not run the desktop UI: {err}. Use --headless to run without a window."
                );
                ExitCode::FAILURE
            }
        }
    }
}

fn run_event_loop(
    manager: Arc<Mutex<Manager>>,
    events: Receiver<AppEvent>,
) -> Result<(), PlatformError> {
    let controller = Controller::new(manager, ManagerWindow::new()?, Some(ManagerTray::new()?));
    controller.bind();
    forward_app_events(&controller, events)?;
    controller.launch()?;
    slint::run_event_loop_until_quit()
}

/// Deliver activation and shutdown requests to the UI thread.
fn forward_app_events(
    controller: &Rc<Controller>,
    events: Receiver<AppEvent>,
) -> Result<(), PlatformError> {
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel();
    std::thread::Builder::new()
        .name("app-events".to_owned())
        .spawn(move || {
            for event in events {
                if sender.send(event).is_err() {
                    break;
                }
            }
        })
        .map_err(|err| PlatformError::Other(err.to_string()))?;
    let controller = Rc::downgrade(controller);
    slint::spawn_local(async move {
        while let Some(event) = receiver.recv().await {
            let Some(controller) = controller.upgrade() else {
                break;
            };
            match event {
                AppEvent::Activate => {
                    tracing::info!("Another launch asked to show the manager");
                    controller.show_window();
                }
                AppEvent::Shutdown => controller.quit(),
            }
        }
    })
    .map_err(|err| PlatformError::Other(err.to_string()))?;
    Ok(())
}

fn shut_down(manager: &Mutex<Manager>) {
    tracing::info!("Shutting down");
    lock(manager).stop();
}

fn lock(manager: &Mutex<Manager>) -> MutexGuard<'_, Manager> {
    manager.lock().unwrap_or_else(PoisonError::into_inner)
}

#[derive(Debug, Clone, Copy)]
enum ServiceAction {
    Start,
    Stop,
    Restart,
}

type JobOutcome = Result<String, String>;

#[derive(Debug, Default)]
struct ViewState {
    view: ServiceView,
    /// Banner describing the current view, restored after a transient message.
    mode_banner: String,
    last_running: Option<bool>,
    selected: Option<String>,
    services: Vec<ServiceSnapshot>,
    log_names: Vec<String>,
    /// Configuration text as last loaded or saved, for the dirty marker.
    loaded_config: String,
}

struct Controller {
    manager: Arc<Mutex<Manager>>,
    window: ManagerWindow,
    /// `None` only in tests: the macOS tray needs the process main thread.
    tray: Option<ManagerTray>,
    refresh_timer: Timer,
    busy: Cell<bool>,
    quitting: Cell<bool>,
    tray_visible: Cell<bool>,
    state: RefCell<ViewState>,
}

impl Controller {
    fn new(
        manager: Arc<Mutex<Manager>>,
        window: ManagerWindow,
        tray: Option<ManagerTray>,
    ) -> Rc<Self> {
        window.set_version(crate::VERSION.into());
        window.set_mono_font(MONO_FONT.into());
        install_toml_syntax(&window);
        Rc::new(Self {
            manager,
            window,
            tray,
            refresh_timer: Timer::default(),
            busy: Cell::new(false),
            quitting: Cell::new(false),
            tray_visible: Cell::new(false),
            state: RefCell::default(),
        })
    }

    /// Wrap a handler so UI callbacks hold only a weak reference.
    fn handler(self: &Rc<Self>, action: impl Fn(&Rc<Self>) + 'static) -> impl Fn() + 'static {
        let controller = Rc::downgrade(self);
        move || {
            if let Some(controller) = controller.upgrade() {
                action(&controller);
            }
        }
    }

    fn bind(self: &Rc<Self>) {
        let window = &self.window;
        window.on_quit(self.handler(|c| c.quit()));
        window.on_edit_config(self.handler(|c| c.set_view(ServiceView::Edit)));
        window.on_cancel_edit(self.handler(|c| c.cancel_edit()));
        window.on_apply(self.handler(|c| c.apply()));
        window.on_validate(self.handler(|c| {
            c.validate();
        }));
        window.on_save(self.handler(|c| {
            if c.persist() {
                c.set_status("saved ✓", Level::Success);
            }
        }));
        window.on_start_service(self.handler(|c| c.service_action(ServiceAction::Start)));
        window.on_stop_service(self.handler(|c| c.service_action(ServiceAction::Stop)));
        window.on_restart_service(self.handler(|c| c.service_action(ServiceAction::Restart)));
        window.on_log_selected(self.handler(|c| c.refresh_logs()));
        window.on_refresh_logs(self.handler(|c| c.refresh_logs()));
        window.on_reload_routes(self.handler(|c| c.reload_routes()));

        let controller = Rc::downgrade(self);
        window.on_tab_changed(move |tab| {
            if let Some(c) = controller.upgrade() {
                match tab {
                    LOGS_TAB => c.refresh_logs(),
                    ROUTES_TAB => c.reload_routes(),
                    _ => {}
                }
            }
        });
        let controller = Rc::downgrade(self);
        window.on_editor_edited(move |text| {
            if let Some(c) = controller.upgrade() {
                let dirty = text.as_str() != c.state.borrow().loaded_config;
                c.window.set_dirty(dirty);
            }
        });
        let controller = Rc::downgrade(self);
        window.on_service_selected(move |index| {
            if let Some(c) = controller.upgrade() {
                c.select_service(index);
            }
        });
        let controller = Rc::downgrade(self);
        window.on_open_service_logs(move |index| {
            if let Some(c) = controller.upgrade() {
                c.open_service_logs(index);
            }
        });
        let controller = Rc::downgrade(self);
        window.window().on_close_requested(move || {
            if let Some(c) = controller.upgrade() {
                if !c.tray_visible.get() {
                    // Without a tray icon a hidden window could not be reopened.
                    c.quit();
                }
                dock::set_icon_visible(false);
            }
            CloseRequestResponse::HideWindow
        });

        if let Some(tray) = &self.tray {
            tray.on_open_manager(self.handler(|c| c.show_window()));
            tray.on_quit(self.handler(|c| c.quit()));
        }
    }

    /// Show the UI and start the configuration on disk, if there is one.
    fn launch(self: &Rc<Self>) -> Result<(), PlatformError> {
        self.window.show()?;
        match self.tray.as_ref().map(ManagerTray::show) {
            Some(Ok(())) => self.tray_visible.set(true),
            Some(Err(err)) => {
                tracing::warn!("No system tray icon ({err}); closing the window quits");
            }
            None => {}
        }
        self.refresh_timer.start(
            TimerMode::Repeated,
            REFRESH_INTERVAL,
            self.handler(|c| c.refresh()),
        );

        let has_config = self.with_manager(Manager::has_config).unwrap_or(false);
        if has_config {
            self.window.set_view(ServiceView::Services);
            self.job("starting…".to_owned(), |manager| {
                manager
                    .start()
                    .map(|_| "started ✓".to_owned())
                    .map_err(|err| {
                        tracing::error!("Startup failed; services left stopped: {err}");
                        format!("start failed: {err}")
                    })
            });
        } else {
            tracing::warn!("No configuration yet; opening the editor");
            self.reload_routes();
            self.refresh();
        }
        Ok(())
    }

    fn show_window(&self) {
        if self.quitting.get() {
            return;
        }
        self.window.window().set_minimized(false);
        if let Err(err) = self.window.show() {
            tracing::warn!("Could not show the manager window: {err}");
        }
        dock::set_icon_visible(true);
    }

    fn quit(&self) {
        if self.quitting.replace(true) {
            return;
        }
        self.refresh_timer.stop();
        if let Some(tray) = &self.tray {
            let _ = tray.hide();
        }
        let _ = self.window.hide();
        let _ = slint::quit_event_loop();
    }

    /// Run `action` against the manager unless a background job holds it.
    fn with_manager<T>(&self, action: impl FnOnce(&Manager) -> T) -> Option<T> {
        match self.manager.try_lock() {
            Ok(manager) => Some(action(&manager)),
            Err(TryLockError::Poisoned(poisoned)) => Some(action(&poisoned.into_inner())),
            Err(TryLockError::WouldBlock) => None,
        }
    }

    /// Run a blocking manager operation on a worker thread; the outcome
    /// becomes the status line. Controls stay disabled until it finishes.
    fn job(
        self: &Rc<Self>,
        pending: String,
        work: impl FnOnce(&mut Manager) -> JobOutcome + Send + 'static,
    ) {
        if self.quitting.get() || self.busy.replace(true) {
            return;
        }
        self.window.set_busy(true);
        self.set_status(&pending, Level::Neutral);

        let (sender, receiver) = tokio::sync::oneshot::channel();
        let manager = Arc::clone(&self.manager);
        let spawned = std::thread::Builder::new()
            .name("manager-job".to_owned())
            .spawn(move || {
                // Release the manager before waking the UI, so its refresh
                // after the job can take the lock.
                let outcome = work(&mut lock(&manager));
                let _ = sender.send(outcome);
            });
        if let Err(err) = spawned {
            self.finish_job(Err(format!("could not start a worker thread: {err}")));
            return;
        }
        let controller = Rc::downgrade(self);
        let waiting = slint::spawn_local(async move {
            let outcome = receiver
                .await
                .unwrap_or_else(|_| Err("the operation failed unexpectedly".to_owned()));
            if let Some(controller) = controller.upgrade() {
                controller.finish_job(outcome);
            }
        });
        if let Err(err) = waiting {
            tracing::error!("Could not wait for a background operation: {err}");
        }
    }

    fn finish_job(&self, outcome: JobOutcome) {
        self.busy.set(false);
        self.window.set_busy(false);
        match outcome {
            Ok(message) => self.set_status(&message, Level::Success),
            Err(message) => self.set_status(&message, Level::Error),
        }
        self.reload_routes();
        self.refresh();
    }

    fn set_status(&self, text: &str, level: Level) {
        self.window.set_status_text(text.into());
        self.window.set_status_level(level);
    }

    fn set_banner(&self, text: &str, level: Level) {
        self.window.set_banner(text.into());
        self.window.set_banner_level(level);
    }

    fn restore_mode_banner(&self) {
        let banner = self.state.borrow().mode_banner.clone();
        self.set_banner(&banner, Level::Neutral);
    }

    // --- periodic refresh -----------------------------------------------------

    fn refresh(&self) {
        let Some((running, services, log_names)) = self.with_manager(|manager| {
            (
                manager.is_running(),
                manager.services(),
                manager.log_names(),
            )
        }) else {
            return;
        };
        self.sync_run_state(running);
        self.show_services(services);
        self.show_log_names(log_names);
        if running && self.window.get_log_follow() {
            self.refresh_logs();
        }
    }

    /// Switch to the service list when services start and to the editor when
    /// they stop.
    fn sync_run_state(&self, running: bool) {
        let changed = {
            let mut state = self.state.borrow_mut();
            let changed = state.last_running != Some(running);
            state.last_running = Some(running);
            changed
        };
        if changed {
            self.window.set_running(running);
            self.set_view(if running {
                ServiceView::Services
            } else {
                ServiceView::Edit
            });
        }
    }

    fn set_view(&self, view: ServiceView) {
        let banner = match view {
            ServiceView::Services => String::new(),
            ServiceView::Edit => {
                self.window.set_current_tab(SERVICES_TAB);
                if !self.load_config() {
                    "No services.toml exists yet. Enter a configuration, then Save or Start All \
                     to create it."
                        .to_owned()
                } else if self.window.get_running() {
                    "Editing configuration — services keep running. Apply validates and saves \
                     it, and restarts everything only if the configuration changed."
                        .to_owned()
                } else {
                    "Editing configuration — Start All validates, saves, and launches it."
                        .to_owned()
                }
            }
        };
        {
            let mut state = self.state.borrow_mut();
            state.view = view;
            state.mode_banner = banner;
        }
        self.window.set_view(view);
        self.restore_mode_banner();
    }

    fn show_services(&self, services: Vec<ServiceSnapshot>) {
        let mut state = self.state.borrow_mut();
        if state.services != services {
            let rows: Vec<ServiceRow> = services.iter().map(service_row).collect();
            self.window.set_services(ModelRc::from(rows.as_slice()));
            state.services = services;
        }
        let index = state
            .selected
            .as_ref()
            .and_then(|name| state.services.iter().position(|s| &s.name == name));
        if index.is_none() {
            state.selected = None;
        }
        self.window.set_selected_service(to_index(index));
    }

    fn select_service(&self, index: i32) {
        let name = {
            let state = self.state.borrow();
            usize::try_from(index)
                .ok()
                .and_then(|index| state.services.get(index))
                .map(|service| service.name.clone())
        };
        let selected = name.is_some();
        self.state.borrow_mut().selected = name;
        self.window
            .set_selected_service(if selected { index } else { -1 });
    }

    fn service_action(self: &Rc<Self>, action: ServiceAction) {
        let Some(name) = self.state.borrow().selected.clone() else {
            self.set_banner("Select a service first.", Level::Error);
            return;
        };
        self.restore_mode_banner();
        let (pending, done) = match action {
            ServiceAction::Start => ("starting", "started"),
            ServiceAction::Stop => ("stopping", "stopped"),
            ServiceAction::Restart => ("restarting", "restarted"),
        };
        self.job(format!("{pending} {name}…"), move |manager| {
            let result = match action {
                ServiceAction::Start => manager.start_service(&name),
                ServiceAction::Stop => manager.stop_service(&name),
                ServiceAction::Restart => manager.restart_service(&name),
            };
            result
                .map(|()| format!("{name} {done} ✓"))
                .map_err(|err| format!("Error: {err}"))
        });
    }

    // --- lifecycle --------------------------------------------------------------

    /// Save the editor text, then start it, or restart with it only if it
    /// differs from the running configuration.
    fn apply(self: &Rc<Self>) {
        if !self.persist() {
            return;
        }
        if self.window.get_running() {
            // A failed restart leaves everything stopped, which brings the
            // editor back.
            self.set_view(ServiceView::Services);
        }
        self.job("applying…".to_owned(), |manager| {
            manager
                .apply()
                .map(|applied| {
                    match applied {
                        Applied::Started => "saved & started ✓",
                        Applied::Restarted => "saved & restarted ✓",
                        Applied::Unchanged => "saved — no changes, nothing restarted",
                    }
                    .to_owned()
                })
                .map_err(|err| format!("apply failed: {err}"))
        });
    }

    // --- configuration --------------------------------------------------------------

    /// Load the configuration on disk into the editor; returns whether a
    /// file exists.
    fn load_config(&self) -> bool {
        let Some(result) = self.with_manager(Manager::read_config) else {
            return false;
        };
        let (text, exists) = match result {
            Ok(Some(text)) => (text, true),
            Ok(None) => {
                self.set_status("new configuration", Level::Neutral);
                (String::new(), false)
            }
            Err(err) => {
                self.set_status(&format!("read error: {err}"), Level::Error);
                (String::new(), false)
            }
        };
        self.window.set_editor_text(text.as_str().into());
        self.window.set_dirty(false);
        self.state.borrow_mut().loaded_config = text;
        exists
    }

    /// Discard unsaved edits and go back to the service list.
    fn cancel_edit(&self) {
        if self.window.get_dirty() {
            self.set_status("edits discarded", Level::Neutral);
        }
        self.load_config();
        self.set_view(ServiceView::Services);
    }

    fn validate(&self) -> bool {
        let text = self.window.get_editor_text();
        let Some(result) = self.with_manager(|manager| manager.validate_config(&text)) else {
            return false;
        };
        match result {
            Ok(_) => {
                self.set_status("valid ✓", Level::Success);
                self.restore_mode_banner();
                true
            }
            Err(err) => {
                self.set_status("invalid", Level::Error);
                self.set_banner(&err.to_string(), Level::Error);
                false
            }
        }
    }

    /// Validate and save the editor text; returns whether it was written.
    fn persist(&self) -> bool {
        let text = self.window.get_editor_text();
        let Some(result) = self.with_manager(|manager| manager.save_config(&text)) else {
            return false;
        };
        match result {
            Ok(()) => {
                self.state.borrow_mut().loaded_config = text.into();
                self.window.set_dirty(false);
                self.restore_mode_banner();
                true
            }
            Err(ManagerError::Config(err)) => {
                self.set_status("invalid", Level::Error);
                self.set_banner(&err.to_string(), Level::Error);
                false
            }
            Err(err) => {
                self.set_status(&err.to_string(), Level::Error);
                false
            }
        }
    }

    // --- logs -----------------------------------------------------------------------

    fn open_service_logs(&self, index: i32) {
        let name = {
            let state = self.state.borrow();
            usize::try_from(index)
                .ok()
                .and_then(|index| state.services.get(index))
                .map(|service| service.name.clone())
        };
        let Some(name) = name else {
            return;
        };
        let Some(names) = self.with_manager(Manager::log_names) else {
            return;
        };
        let Some(position) = names.iter().position(|log| *log == name) else {
            return;
        };
        self.show_log_names(names);
        self.window.set_log_index(to_index(Some(position)));
        self.window.set_current_tab(LOGS_TAB);
        self.refresh_logs();
    }

    fn show_log_names(&self, names: Vec<String>) {
        let mut state = self.state.borrow_mut();
        if state.log_names == names {
            return;
        }
        let current = usize::try_from(self.window.get_log_index())
            .ok()
            .and_then(|index| state.log_names.get(index))
            .and_then(|name| names.iter().position(|candidate| candidate == name));
        let model: Vec<slint::SharedString> =
            names.iter().map(|name| name.as_str().into()).collect();
        self.window.set_log_names(ModelRc::from(model.as_slice()));
        self.window.set_log_index(to_index(current.or(Some(0))));
        state.log_names = names;
    }

    fn refresh_logs(&self) {
        let lines = usize::try_from(self.window.get_log_lines()).unwrap_or(0);
        let Some((names, body)) = self.with_manager(|manager| {
            let names = manager.log_names();
            let selected = usize::try_from(self.window.get_log_index())
                .ok()
                .and_then(|index| names.get(index).cloned())
                .or_else(|| names.first().cloned());
            let body = match selected {
                Some(name) => manager
                    .tail_log(&name, lines)
                    .unwrap_or_else(|err| format!("[error] {err}")),
                None => String::new(),
            };
            (names, body)
        }) else {
            return;
        };
        self.show_log_names(names);
        if self.window.get_log_text() != body.as_str() {
            self.window.set_log_text(body.into());
        }
    }

    // --- routes ---------------------------------------------------------------------

    fn reload_routes(&self) {
        let Some(result) = self.with_manager(Manager::routes) else {
            return;
        };
        let rows = match result {
            Ok(groups) => {
                self.window.set_routes_banner("".into());
                route_rows(groups)
            }
            Err(err) => {
                self.window
                    .set_routes_banner(format!("Error: {err}").into());
                Vec::new()
            }
        };
        self.window.set_routes(ModelRc::from(rows.as_slice()));
    }
}

fn to_index(index: Option<usize>) -> i32 {
    index
        .and_then(|index| i32::try_from(index).ok())
        .unwrap_or(-1)
}

fn service_row(service: &ServiceSnapshot) -> ServiceRow {
    let or_dash = |value: Option<String>| value.unwrap_or_else(|| "-".to_owned()).into();
    ServiceRow {
        name: service.name.as_str().into(),
        status: service.status.as_str().into(),
        pid: or_dash(service.pid.map(|pid| pid.to_string())),
        restarts: service.restart_count.to_string().into(),
        exit_code: or_dash(service.exit_code.map(|code| code.to_string())),
        controllable: service.status.is_controllable(),
        running: service.status == ServiceStatus::Running,
    }
}

fn route_rows(groups: Vec<RouteGroup>) -> Vec<RouteRow> {
    let mut rows = Vec::new();
    for group in groups {
        rows.push(RouteRow {
            header: true,
            label: group.service.into(),
            note: group.kind.note().into(),
            muted: group.kind == ServiceKind::Disabled,
            ..RouteRow::default()
        });
        rows.extend(group.entries.into_iter().map(|entry| RouteRow {
            header: false,
            label: entry.route_id.into(),
            note: "".into(),
            muted: entry.url.is_none(),
            url: entry.url.unwrap_or_default().into(),
            host: entry.host.into(),
            target: entry.target.into(),
        }));
    }
    rows
}

/// macOS keeps a Dock tile only while the manager window is visible; the app
/// otherwise lives in the menu bar. A no-op elsewhere.
mod dock {
    #[cfg(target_os = "macos")]
    pub fn set_icon_visible(visible: bool) {
        use objc2::MainThreadMarker;
        use objc2_app_kit::{NSApplication, NSApplicationActivationPolicy};

        let Some(main_thread) = MainThreadMarker::new() else {
            return;
        };
        let app = NSApplication::sharedApplication(main_thread);
        app.setActivationPolicy(if visible {
            NSApplicationActivationPolicy::Regular
        } else {
            NSApplicationActivationPolicy::Accessory
        });
        if visible {
            // `activate` needs macOS 14; this covers older releases too.
            #[allow(deprecated)]
            app.activateIgnoringOtherApps(true);
        }
    }

    #[cfg(not(target_os = "macos"))]
    pub fn set_icon_visible(_visible: bool) {}
}

mod app_exit {
    /// Keeps the terminate handler registered while alive.
    pub struct Registration {
        #[cfg(target_os = "macos")]
        token: objc2::rc::Retained<
            objc2::runtime::ProtocolObject<dyn objc2::runtime::NSObjectProtocol>,
        >,
    }

    /// Run `handler` when AppKit is about to exit the process (`terminate:`),
    /// which it does without returning from the event loop.
    #[cfg(target_os = "macos")]
    pub fn on_terminate(handler: impl Fn() + Send + 'static) -> Registration {
        use std::ptr::NonNull;

        use block2::RcBlock;
        use objc2_app_kit::NSApplicationWillTerminateNotification;
        use objc2_foundation::{NSNotification, NSNotificationCenter};

        let block = RcBlock::new(move |_: NonNull<NSNotification>| handler());
        let center = NSNotificationCenter::defaultCenter();
        // SAFETY: the name is an AppKit constant; with no queue the block
        // runs synchronously on the posting (main) thread, and it is `Send`.
        let token = unsafe {
            center.addObserverForName_object_queue_usingBlock(
                Some(NSApplicationWillTerminateNotification),
                None,
                None,
                &block,
            )
        };
        Registration { token }
    }

    #[cfg(not(target_os = "macos"))]
    pub fn on_terminate(_handler: impl Fn() + Send + 'static) -> Registration {
        Registration {}
    }

    #[cfg(target_os = "macos")]
    impl Drop for Registration {
        fn drop(&mut self) {
            use objc2_foundation::NSNotificationCenter;

            // SAFETY: `token` came from this center's `addObserverForName`.
            unsafe { NSNotificationCenter::defaultCenter().removeObserver(self.token.as_ref()) };
        }
    }
}

fn install_toml_syntax(window: &ManagerWindow) {
    let syntax = window.global::<TomlSyntax>();
    syntax.on_tokenize(|text| {
        let tokens: Vec<TomlToken> = syntax::tokenize(&text)
            .into_iter()
            .map(|token| TomlToken {
                line: to_int(token.line),
                column: to_int(token.column),
                text: token.text.into(),
                kind: match token.kind {
                    syntax::Kind::Plain => TomlTokenKind::Plain,
                    syntax::Kind::Comment => TomlTokenKind::Comment,
                    syntax::Kind::Table => TomlTokenKind::Table,
                    syntax::Kind::Key => TomlTokenKind::Key,
                    syntax::Kind::String => TomlTokenKind::String,
                    syntax::Kind::Number => TomlTokenKind::Number,
                    syntax::Kind::Boolean => TomlTokenKind::Boolean,
                    syntax::Kind::Punctuation => TomlTokenKind::Punctuation,
                },
            })
            .collect();
        ModelRc::new(VecModel::from(tokens))
    });
    syntax.on_position(|text, byte_offset| {
        let (line, column) = syntax::position(&text, usize::try_from(byte_offset).unwrap_or(0));
        TextPosition {
            line: to_int(line),
            column: to_int(column),
        }
    });
}

fn to_int(n: usize) -> i32 {
    i32::try_from(n).unwrap_or(i32::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::routes::RouteEntry;

    #[test]
    fn service_rows_show_dashes_for_missing_values() {
        let row = service_row(&ServiceSnapshot {
            name: "app".to_owned(),
            status: ServiceStatus::Crashed,
            managed: true,
            pid: None,
            exit_code: Some(-15),
            restart_count: 2,
        });
        assert_eq!(row.pid, "-");
        assert_eq!(row.exit_code, "-15");
        assert_eq!(row.restarts, "2");
        assert!(row.controllable);
        assert!(!row.running);
    }

    #[test]
    fn route_rows_put_each_service_header_before_its_hosts() {
        let rows = route_rows(vec![
            RouteGroup {
                service: "web".to_owned(),
                kind: ServiceKind::Managed,
                entries: vec![
                    RouteEntry {
                        route_id: "web".to_owned(),
                        host: "web.localhost".to_owned(),
                        url: Some("http://web.localhost:2800/".to_owned()),
                        target: "localhost:3000".to_owned(),
                    },
                    RouteEntry {
                        route_id: "web".to_owned(),
                        host: "*.web.localhost".to_owned(),
                        url: None,
                        target: "localhost:3000".to_owned(),
                    },
                ],
            },
            RouteGroup {
                service: "off".to_owned(),
                kind: ServiceKind::Disabled,
                entries: Vec::new(),
            },
        ]);
        let summary: Vec<_> = rows
            .iter()
            .map(|row| (row.header, row.label.as_str(), row.url.as_str(), row.muted))
            .collect();
        assert_eq!(
            summary,
            [
                (true, "web", "", false),
                (false, "web", "http://web.localhost:2800/", false),
                (false, "web", "", true),
                (true, "off", "", true),
            ]
        );
        assert_eq!(rows[3].note, ServiceKind::Disabled.note());
    }

    /// Drives the real window and controller on Slint's headless testing
    /// backend. The backend can only be initialized once per process, so this
    /// is the only test in the crate that creates Slint components.
    #[cfg(unix)]
    #[test]
    fn manager_window_edits_starts_controls_and_applies_services() {
        use i_slint_backend_testing::ElementHandle;
        use slint::Model;

        use crate::paths::ProjectPaths;

        async fn pause() {
            let (sender, receiver) = tokio::sync::oneshot::channel();
            Timer::single_shot(Duration::from_millis(20), move || {
                let _ = sender.send(());
            });
            let _ = receiver.await;
        }

        async fn wait_until(what: &str, mut condition: impl FnMut() -> bool) {
            let deadline = std::time::Instant::now() + Duration::from_secs(20);
            while !condition() {
                assert!(
                    std::time::Instant::now() < deadline,
                    "timed out waiting for {what}"
                );
                pause().await;
            }
        }

        fn click(window: &ManagerWindow, label: &str) {
            let button = ElementHandle::find_by_accessible_label(window, label)
                .next()
                .unwrap_or_else(|| panic!("no element labelled {label:?}"));
            assert_eq!(
                button.accessible_enabled(),
                Some(true),
                "{label} is disabled"
            );
            button.invoke_accessible_default_action();
        }

        fn type_config(window: &ManagerWindow, text: &str) {
            ElementHandle::find_by_accessible_label(window, "Configuration editor")
                .next()
                .expect("the configuration editor")
                .set_accessible_value(text);
        }

        i_slint_backend_testing::init_integration_test_with_system_time();

        let dir = tempfile::tempdir().unwrap();
        let paths = ProjectPaths::new(dir.path()).unwrap().ensure().unwrap();
        let services_file = paths.services_file();
        let manager = Arc::new(Mutex::new(Manager::new(paths).unwrap()));
        let port = std::net::TcpListener::bind("127.0.0.1:0")
            .unwrap()
            .local_addr()
            .unwrap()
            .port();
        let config_on = |port: u16| {
            format!(
                "http_port = {port}\nbind = [\"127.0.0.1\"]\n\n\
                 [services.app]\ncommand = [\"sh\", \"-c\", \"echo hello from app; exec sleep 30\"]\n\n\
                 [[services.app.routes]]\nid = \"app\"\nhosts = [\"app.localhost\"]\ntarget_port = 1\n\n\
                 [services.external]\n"
            )
        };
        let config = config_on(port);
        // The changed configuration must differ, so the port must too.
        let new_port = loop {
            let candidate = std::net::TcpListener::bind("127.0.0.1:0")
                .unwrap()
                .local_addr()
                .unwrap()
                .port();
            if candidate != port {
                break candidate;
            }
        };

        let controller = Controller::new(Arc::clone(&manager), ManagerWindow::new().unwrap(), None);
        controller.bind();
        controller.launch().unwrap();

        let script = {
            let controller = Rc::clone(&controller);
            async move {
                let window = &controller.window;

                // No configuration yet: the editor opens with guidance.
                wait_until("the editor", || window.get_view() == ServiceView::Edit).await;
                assert!(
                    window
                        .get_banner()
                        .starts_with("No services.toml exists yet")
                );

                // Cancel goes back to the service list while stopped, too.
                type_config(window, "# discarded\n");
                click(window, "Cancel");
                assert_eq!(window.get_view(), ServiceView::Services);
                click(window, "Edit Config");
                assert_eq!(window.get_view(), ServiceView::Edit);
                assert_eq!(window.get_editor_text(), "");

                type_config(window, "http_port = \"x\"\nbind = [\"127.0.0.1\"]");
                assert!(window.get_dirty());
                click(window, "Validate");
                assert_eq!(window.get_status_text(), "invalid");
                assert_eq!(window.get_banner_level(), Level::Error);
                assert!(
                    window.get_banner().contains("http_port"),
                    "{}",
                    window.get_banner()
                );

                type_config(window, &config);
                click(window, "Validate");
                assert_eq!(window.get_status_text(), "valid ✓");

                click(window, "Start All");
                wait_until("start", || !window.get_busy()).await;
                assert_eq!(window.get_status_text(), "saved & started ✓");
                assert_eq!(std::fs::read_to_string(&services_file).unwrap(), config);
                assert_eq!(window.get_view(), ServiceView::Services);
                assert!(!window.get_dirty());
                std::net::TcpStream::connect(("127.0.0.1", port)).unwrap();

                let statuses: Vec<_> = window
                    .get_services()
                    .iter()
                    .map(|row| (row.name.to_string(), row.status.to_string()))
                    .collect();
                assert_eq!(
                    statuses,
                    [
                        ("app".to_owned(), "running".to_owned()),
                        ("external".to_owned(), "unmanaged".to_owned()),
                    ]
                );
                let headers: Vec<_> = window
                    .get_routes()
                    .iter()
                    .filter(|row| row.header)
                    .map(|row| row.label.to_string())
                    .collect();
                assert_eq!(headers, ["app", "external"]);

                // Unmanaged services cannot be controlled.
                window.invoke_service_selected(1);
                assert_eq!(window.get_selected_service(), 1);
                assert!(
                    ElementHandle::find_by_accessible_label(window, "Start")
                        .next()
                        .and_then(|button| button.accessible_enabled())
                        == Some(false)
                );

                window.invoke_service_selected(0);
                click(window, "Restart");
                wait_until("restart", || !window.get_busy()).await;
                assert_eq!(window.get_status_text(), "app restarted ✓");
                assert_eq!(window.get_services().row_data(0).unwrap().restarts, "1");

                window.invoke_open_service_logs(0);
                assert_eq!(window.get_current_tab(), LOGS_TAB);
                wait_until("the log", || {
                    controller.refresh_logs();
                    window.get_log_text().contains("hello from app")
                })
                .await;

                // Editing leaves the services running.
                let pid = window.get_services().row_data(0).unwrap().pid;
                window.set_current_tab(LOGS_TAB);
                window.set_current_tab(SERVICES_TAB);
                click(window, "Edit Config");
                assert_eq!(window.get_view(), ServiceView::Edit);
                assert_eq!(window.get_editor_text(), config.as_str());
                assert!(window.get_banner().contains("services keep running"));
                assert!(window.get_running());

                // Cancel discards unsaved edits and goes back to the list.
                type_config(window, "# discarded\n");
                assert!(window.get_dirty());
                click(window, "Cancel");
                assert_eq!(window.get_view(), ServiceView::Services);
                assert_eq!(window.get_status_text(), "edits discarded");
                assert_eq!(std::fs::read_to_string(&services_file).unwrap(), config);
                click(window, "Edit Config");
                assert!(!window.get_dirty());
                assert_eq!(window.get_editor_text(), config.as_str());
                type_config(window, &format!("# only a comment\n{config}"));

                // A comment is no change: saved, but nothing restarts.
                click(window, "Apply");
                wait_until("apply", || !window.get_busy()).await;
                assert_eq!(
                    window.get_status_text(),
                    "saved — no changes, nothing restarted"
                );
                assert_eq!(window.get_view(), ServiceView::Services);
                assert!(
                    std::fs::read_to_string(&services_file)
                        .unwrap()
                        .starts_with("# only a comment")
                );
                assert_eq!(window.get_services().row_data(0).unwrap().pid, pid);

                // A real change restarts everything with it.
                click(window, "Edit Config");
                type_config(window, &config_on(new_port));
                click(window, "Apply");
                wait_until("apply", || !window.get_busy()).await;
                assert_eq!(window.get_status_text(), "saved & restarted ✓");
                assert_eq!(window.get_view(), ServiceView::Services);
                assert_ne!(window.get_services().row_data(0).unwrap().pid, pid);
                std::net::TcpStream::connect(("127.0.0.1", new_port)).unwrap();
                assert!(std::net::TcpStream::connect(("127.0.0.1", port)).is_err());

                click(window, "Quit");
            }
        };
        slint::spawn_local(script).unwrap();
        slint::run_event_loop_until_quit().unwrap();

        // Quitting only ends the event loop; `Desktop::run` then stops
        // everything, as here.
        let mut manager = lock(&manager);
        assert!(manager.is_running());
        manager.stop();
        assert!(
            manager
                .services()
                .iter()
                .all(|service| service.status != ServiceStatus::Running)
        );
    }
}
