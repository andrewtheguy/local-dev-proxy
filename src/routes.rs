//! Resolution of configured routes into concrete proxy targets.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::config::{
    ConfigError, Manifest, RouteTarget, ServiceDef, ServiceRoute, require_port, require_socket_path,
};

/// Lookup for process environment values that override service `env` tables.
pub type EnvLookup<'a> = &'a dyn Fn(&str) -> Option<String>;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ResolvedTarget {
    Tcp { host: String, port: u16 },
    Unix(PathBuf),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedRoute {
    pub id: String,
    pub host_patterns: Vec<String>,
    pub target: ResolvedTarget,
}

/// Look up `key` with the process environment taking precedence over the
/// service's own `env` table.
fn service_value(service: &ServiceDef, env: EnvLookup<'_>, key: &str) -> Option<String> {
    env(key).or_else(|| service.env.get(key).cloned())
}

/// Resolve every enabled route to a concrete target.
///
/// Relative socket paths are resolved against `socket_base_dir`. Two routes
/// may not share a TCP port or socket path.
pub fn resolve_routes(
    manifest: &Manifest,
    env: EnvLookup<'_>,
    socket_base_dir: Option<&Path>,
) -> Result<Vec<ResolvedRoute>, ConfigError> {
    let mut resolved = Vec::new();
    let mut seen_ports: HashMap<u16, String> = HashMap::new();
    let mut seen_sockets: HashMap<PathBuf, String> = HashMap::new();

    for service in manifest.services.iter().filter(|service| !service.disabled) {
        for route in &service.routes {
            let target = match &route.target {
                RouteTarget::Socket(_) | RouteTarget::SocketEnv(_) => {
                    let raw = match &route.target {
                        RouteTarget::Socket(path) => path.clone(),
                        RouteTarget::SocketEnv(var) => {
                            require_socket_path(service_value(service, env, var).as_deref(), var)?
                        }
                        _ => unreachable!(),
                    };
                    let path = resolve_socket_path(&raw, socket_base_dir);
                    if let Some(owner) = seen_sockets.get(&path) {
                        return Err(ConfigError(format!(
                            "Socket {:?} used by both {owner} and {}",
                            path.display().to_string(),
                            route.id
                        )));
                    }
                    seen_sockets.insert(path.clone(), route.id.clone());
                    ResolvedTarget::Unix(path)
                }
                RouteTarget::Port { host, port } => {
                    claim_port(&mut seen_ports, *port, &route.id)?;
                    ResolvedTarget::Tcp {
                        host: host.clone(),
                        port: *port,
                    }
                }
                RouteTarget::PortEnv { host, var } => {
                    let port = require_port(service_value(service, env, var).as_deref(), var)?;
                    claim_port(&mut seen_ports, port, &route.id)?;
                    ResolvedTarget::Tcp {
                        host: host.clone(),
                        port,
                    }
                }
            };
            resolved.push(ResolvedRoute {
                id: route.id.clone(),
                host_patterns: route.hosts.clone(),
                target,
            });
        }
    }
    Ok(resolved)
}

fn claim_port(seen: &mut HashMap<u16, String>, port: u16, id: &str) -> Result<(), ConfigError> {
    if let Some(owner) = seen.get(&port) {
        return Err(ConfigError(format!(
            "Port {port} used by both {owner} and {id}"
        )));
    }
    seen.insert(port, id.to_owned());
    Ok(())
}

fn resolve_socket_path(raw: &str, base: Option<&Path>) -> PathBuf {
    let path = PathBuf::from(raw);
    match base {
        Some(base) if path.is_relative() => {
            let joined = base.join(path);
            std::path::absolute(&joined).unwrap_or(joined)
        }
        _ => path,
    }
}

/// Whether a route host is a glob pattern rather than one exact hostname.
pub fn is_wildcard_host(host: &str) -> bool {
    host.contains(['*', '?', '['])
}

/// Immutable host matcher precompiled from a route list.
///
/// Exact hostnames resolve with one map lookup; wildcard patterns (`*`, `?`,
/// `[...]`) are tried in declaration order. Exact matches win over wildcards
/// regardless of declaration order. Matching is case-insensitive.
#[derive(Debug)]
pub struct HostMatcher {
    exact: HashMap<String, usize>,
    wildcards: Vec<(glob::Pattern, usize)>,
}

impl HostMatcher {
    pub fn new(routes: &[ResolvedRoute]) -> Self {
        let mut exact = HashMap::new();
        let mut wildcards = Vec::new();
        for (index, route) in routes.iter().enumerate() {
            for pattern in &route.host_patterns {
                let pattern = pattern.to_ascii_lowercase();
                if is_wildcard_host(&pattern) {
                    let compiled = glob::Pattern::new(&pattern).unwrap_or_else(|_| {
                        glob::Pattern::new(&glob::Pattern::escape(&pattern))
                            .expect("escaped pattern is valid")
                    });
                    wildcards.push((compiled, index));
                } else {
                    exact.entry(pattern).or_insert(index);
                }
            }
        }
        Self { exact, wildcards }
    }

    /// Index of the route that serves `host`.
    pub fn find(&self, host: &str) -> Option<usize> {
        let host = host.to_ascii_lowercase();
        if let Some(index) = self.exact.get(&host) {
            return Some(*index);
        }
        self.wildcards
            .iter()
            .find(|(pattern, _)| pattern.matches(&host))
            .map(|(_, index)| *index)
    }
}

/// The resolved routes, their matcher, and the portal page for one config.
#[derive(Debug)]
pub struct RouteTable {
    routes: Vec<ResolvedRoute>,
    matcher: HostMatcher,
    portal_html: String,
}

impl RouteTable {
    /// Build a table whose portal links use `http_port`.
    pub fn new(http_port: u16, routes: Vec<ResolvedRoute>) -> Self {
        let matcher = HostMatcher::new(&routes);
        let portal_html = build_portal_html(http_port, &routes);
        Self {
            routes,
            matcher,
            portal_html,
        }
    }

    /// Resolve `manifest` against the process environment, with relative socket
    /// paths anchored at `base_dir` (the directory holding `services.toml`).
    pub fn from_manifest(
        manifest: &Manifest,
        env: EnvLookup<'_>,
        base_dir: &Path,
    ) -> Result<Self, ConfigError> {
        let routes = resolve_routes(manifest, env, Some(base_dir))?;
        Ok(Self::new(manifest.http_port, routes))
    }

    pub fn routes(&self) -> &[ResolvedRoute] {
        &self.routes
    }

    pub fn portal_html(&self) -> &str {
        &self.portal_html
    }

    pub fn find(&self, host: &str) -> Option<&ResolvedRoute> {
        self.matcher.find(host).map(|index| &self.routes[index])
    }
}

fn build_portal_html(http_port: u16, routes: &[ResolvedRoute]) -> String {
    let items: Vec<String> = routes
        .iter()
        .flat_map(|route| &route.host_patterns)
        .map(|host| {
            if is_wildcard_host(host) {
                format!("<li>{}</li>", escape_html(host))
            } else {
                let url = format!("http://{host}:{http_port}/");
                format!(
                    "<li><a href=\"{}\">{}</a></li>",
                    escape_html(&url),
                    escape_html(host)
                )
            }
        })
        .collect();
    format!(
        "<!doctype html><html><head><meta charset='utf-8'>\
         <title>Local Dev Proxy</title></head><body>\
         <h1>Local Dev Proxy</h1><ul>{}</ul></body></html>",
        items.join("\n")
    )
}

fn escape_html(text: &str) -> String {
    let mut escaped = String::with_capacity(text.len());
    for ch in text.chars() {
        match ch {
            '&' => escaped.push_str("&amp;"),
            '<' => escaped.push_str("&lt;"),
            '>' => escaped.push_str("&gt;"),
            '"' => escaped.push_str("&quot;"),
            '\'' => escaped.push_str("&#x27;"),
            _ => escaped.push(ch),
        }
    }
    escaped
}

/// A route host as presented to a user interface.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteEntry {
    pub route_id: String,
    pub host: String,
    /// Browser URL for an exact host; `None` for a wildcard pattern.
    pub url: Option<String>,
    /// Human-readable target, e.g. `localhost:3000` or `unix:app.sock`.
    pub target: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ServiceKind {
    Managed,
    /// Managed, but not launched with the others.
    ManualStart,
    /// No command: another tool owns the process.
    External,
    /// Not started and routes inactive.
    Disabled,
}

impl ServiceKind {
    pub fn of(service: &ServiceDef) -> Self {
        if service.disabled {
            Self::Disabled
        } else if service.command.is_none() {
            Self::External
        } else if !service.auto_start {
            Self::ManualStart
        } else {
            Self::Managed
        }
    }

    pub fn note(self) -> &'static str {
        match self {
            Self::Managed => "",
            Self::ManualStart => "manual start — not started with the others",
            Self::External => "external — not started here",
            Self::Disabled => "disabled — routes inactive",
        }
    }
}

/// A service and its route hosts, as listed by a routes view.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteGroup {
    pub service: String,
    pub kind: ServiceKind,
    /// Empty for disabled services, whose routes are inactive.
    pub entries: Vec<RouteEntry>,
}

/// Describe every service's routes for display, without resolving the
/// process environment (unset variables are shown as `${VAR}`).
pub fn route_listing(manifest: &Manifest) -> Vec<RouteGroup> {
    manifest
        .services
        .iter()
        .map(|service| {
            let kind = ServiceKind::of(service);
            let entries = if kind == ServiceKind::Disabled {
                Vec::new()
            } else {
                service
                    .routes
                    .iter()
                    .flat_map(|route| route_entries(service, route, manifest.http_port))
                    .collect()
            };
            RouteGroup {
                service: service.name.clone(),
                kind,
                entries,
            }
        })
        .collect()
}

fn route_entries(service: &ServiceDef, route: &ServiceRoute, http_port: u16) -> Vec<RouteEntry> {
    let target = describe_target(service, &route.target);
    route
        .hosts
        .iter()
        .map(|host| RouteEntry {
            route_id: route.id.clone(),
            host: host.clone(),
            url: (!is_wildcard_host(host)).then(|| format!("http://{host}:{http_port}/")),
            target: target.clone(),
        })
        .collect()
}

fn describe_target(service: &ServiceDef, target: &RouteTarget) -> String {
    let from_env = |var: &str| {
        service
            .env
            .get(var)
            .filter(|value| !value.is_empty())
            .cloned()
            .unwrap_or_else(|| format!("${{{var}}}"))
    };
    match target {
        RouteTarget::Socket(path) => format!("unix:{path}"),
        RouteTarget::SocketEnv(var) => format!("unix:{}", from_env(var)),
        RouteTarget::Port { host, port } => format!("{host}:{port}"),
        RouteTarget::PortEnv { host, var } => format!("{host}:{}", from_env(var)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::parse_manifest;

    const SERVICES_TOML: &str = r#"
http_port = 2800
bind = ["127.0.0.1"]

[services.minio]
command = ["minio"]
env = {MINIO_PORT = "19000", MINIO_CONSOLE_PORT = "19001"}

[[services.minio.routes]]
id = "minio"
hosts = ["minios3.localhost", "*.minios3.localhost"]
target_port_env = "MINIO_PORT"

[[services.minio.routes]]
id = "minioconsole"
hosts = ["minioconsole.localhost"]
target_port_env = "MINIO_CONSOLE_PORT"

[services.s3browser]
command = ["s3browser"]
env = {S3BROWSER_PORT = "18170"}

[[services.s3browser.routes]]
id = "s3browser"
hosts = ["s3browser.localhost"]
target_port_env = "S3BROWSER_PORT"
"#;

    fn no_env(_: &str) -> Option<String> {
        None
    }

    fn tcp(port: u16) -> ResolvedTarget {
        ResolvedTarget::Tcp {
            host: "localhost".into(),
            port,
        }
    }

    fn route(id: &str, hosts: &[&str], port: u16) -> ResolvedRoute {
        ResolvedRoute {
            id: id.into(),
            host_patterns: hosts.iter().map(|h| h.to_string()).collect(),
            target: tcp(port),
        }
    }

    #[test]
    fn resolve_routes_resolves_ports() {
        let manifest = parse_manifest(SERVICES_TOML).unwrap();
        let routes = resolve_routes(&manifest, &no_env, None).unwrap();
        let summary: Vec<_> = routes.iter().map(|r| (r.id.as_str(), &r.target)).collect();
        assert_eq!(
            summary,
            [
                ("minio", &tcp(19000)),
                ("minioconsole", &tcp(19001)),
                ("s3browser", &tcp(18170))
            ]
        );
    }

    #[test]
    fn resolve_routes_environment_overrides_service_env() {
        let manifest = parse_manifest(SERVICES_TOML).unwrap();
        let env = |key: &str| (key == "MINIO_PORT").then(|| "29000".to_string());
        let routes = resolve_routes(&manifest, &env, None).unwrap();
        assert_eq!(routes[0].target, tcp(29000));
    }

    #[test]
    fn resolve_routes_rejects_duplicate_port() {
        let manifest = parse_manifest(SERVICES_TOML).unwrap();
        let env = |key: &str| (key == "S3BROWSER_PORT").then(|| "19000".to_string());
        let err = resolve_routes(&manifest, &env, None).unwrap_err();
        assert_eq!(err.0, "Port 19000 used by both minio and s3browser");
    }

    #[test]
    fn resolve_routes_requires_port_variable() {
        let manifest = parse_manifest(
            r#"
http_port = 2800
bind = ["127.0.0.1"]
[services.app]
[[services.app.routes]]
id = "app"
hosts = ["app.localhost"]
target_port_env = "APP_PORT"
"#,
        )
        .unwrap();
        let err = resolve_routes(&manifest, &no_env, None).unwrap_err();
        assert_eq!(err.0, "Missing required port variable: APP_PORT");
    }

    const SOCKETS_TOML: &str = r#"
http_port = 2800
bind = ["127.0.0.1"]

[services.fixed]
[[services.fixed.routes]]
id = "fixed"
hosts = ["fixed.localhost"]
target_socket = "/tmp/fixed.sock"

[services.env]
env = {APP_SOCKET = "run/app.sock"}
[[services.env.routes]]
id = "env"
hosts = ["env.localhost"]
target_socket_env = "APP_SOCKET"
"#;

    #[test]
    fn resolve_routes_resolves_fixed_and_env_sockets() {
        let manifest = parse_manifest(SOCKETS_TOML).unwrap();
        let base = Path::new("/profile");
        let routes = resolve_routes(&manifest, &no_env, Some(base)).unwrap();
        assert_eq!(
            routes[0].target,
            ResolvedTarget::Unix("/tmp/fixed.sock".into())
        );
        assert_eq!(
            routes[1].target,
            ResolvedTarget::Unix(std::path::absolute("/profile/run/app.sock").unwrap())
        );
    }

    #[test]
    fn resolve_routes_rejects_duplicate_socket() {
        let manifest = parse_manifest(SOCKETS_TOML).unwrap();
        let env = |key: &str| (key == "APP_SOCKET").then(|| "/tmp/fixed.sock".to_string());
        let err = resolve_routes(&manifest, &env, None).unwrap_err();
        assert!(err.0.contains("used by both fixed and env"), "{}", err.0);
    }

    #[test]
    fn resolve_routes_requires_socket_variable() {
        let manifest =
            parse_manifest(&SOCKETS_TOML.replace("env = {APP_SOCKET = \"run/app.sock\"}", ""))
                .unwrap();
        let err = resolve_routes(&manifest, &no_env, None).unwrap_err();
        assert_eq!(err.0, "Missing required socket variable: APP_SOCKET");
    }

    #[test]
    fn disabled_services_are_not_routed() {
        let manifest = parse_manifest(
            r#"
http_port = 2800
bind = ["127.0.0.1"]
[services.off]
command = ["off"]
disabled = true
[[services.off.routes]]
id = "off"
hosts = ["off.localhost"]
target_port_env = "UNSET_PORT"
"#,
        )
        .unwrap();
        assert!(resolve_routes(&manifest, &no_env, None).unwrap().is_empty());
    }

    #[test]
    fn matcher_prefers_exact_over_wildcard_and_ignores_case() {
        let routes = vec![
            route("wild", &["*.app.localhost"], 1),
            route("exact", &["api.app.localhost"], 2),
            route("single", &["?.localhost", "[ab]x.localhost"], 3),
        ];
        let matcher = HostMatcher::new(&routes);
        assert_eq!(matcher.find("API.app.localhost"), Some(1));
        assert_eq!(matcher.find("web.app.localhost"), Some(0));
        assert_eq!(matcher.find("x.localhost"), Some(2));
        assert_eq!(matcher.find("bx.localhost"), Some(2));
        assert_eq!(matcher.find("cx.localhost"), None);
        assert_eq!(matcher.find("unknown.localhost"), None);
    }

    #[test]
    fn route_table_portal_links_exact_hosts() {
        let manifest = parse_manifest(SERVICES_TOML).unwrap();
        let table = RouteTable::from_manifest(&manifest, &no_env, Path::new("/")).unwrap();
        let html = table.portal_html();
        assert!(html.contains("<a href=\"http://minios3.localhost:2800/\">minios3.localhost</a>"));
        assert!(html.contains("<li>*.minios3.localhost</li>"));
        for host in ["?.localhost", "[ab].localhost"] {
            let html = build_portal_html(2800, &[route("glob", &[host], 1)]);
            assert!(html.contains(&format!("<li>{host}</li>")), "{html}");
        }
        assert_eq!(table.find("s3browser.localhost").unwrap().id, "s3browser");
    }

    #[test]
    fn route_listing_describes_targets_and_kinds() {
        let manifest = parse_manifest(crate::SAMPLE_CONFIG).unwrap();
        let groups = route_listing(&manifest);
        let by_name = |name: &str| groups.iter().find(|g| g.service == name).unwrap();

        let api = by_name("api");
        assert_eq!(api.kind, ServiceKind::Managed);
        assert_eq!(
            api.entries[0].url.as_deref(),
            Some("http://api.localhost:2800/")
        );
        assert_eq!(api.entries[0].target, "127.0.0.1:8000");
        assert_eq!(api.entries[1].url, None);

        assert_eq!(by_name("frontend").kind, ServiceKind::External);
        assert_eq!(by_name("on_demand").kind, ServiceKind::ManualStart);
        assert_eq!(by_name("optional_service").kind, ServiceKind::Disabled);
        assert!(by_name("optional_service").entries.is_empty());
        assert_eq!(
            by_name("socket_app").entries[0].target,
            "unix:run/my-socket-app.sock"
        );
        assert_eq!(
            by_name("inherited_environment").entries[0].target,
            "::1:${INHERITED_PORT}"
        );
    }
}
