//! Strict parsing and validation of `services.toml`.
//!
//! Values are type-checked without coercion and unknown keys are rejected, so
//! an older or misspelled configuration shape fails loudly instead of being
//! silently reinterpreted.

use std::collections::BTreeMap;
use std::path::Path;

use toml::{Table, Value};

const TOP_LEVEL_KEYS: &[&str] = &["http_port", "bind", "services"];
const SERVICE_KEYS: &[&str] = &["command", "env", "routes", "disabled", "auto_start"];
const ROUTE_KEYS: &[&str] = &[
    "id",
    "hosts",
    "target_host",
    "target_port",
    "target_port_env",
    "target_socket",
    "target_socket_env",
];
const TARGET_KEYS: &[&str] = &[
    "target_port",
    "target_port_env",
    "target_socket",
    "target_socket_env",
];
/// Service names become log file names; `manager` is the application's own log.
const RESERVED_SERVICE_NAMES: &[&str] = &["manager"];
pub const ALLOWED_TARGET_HOSTS: &[&str] = &["127.0.0.1", "::1", "localhost"];
const DEFAULT_TARGET_HOST: &str = "localhost";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ConfigError(pub String);

impl ConfigError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

/// Where a route forwards requests.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RouteTarget {
    /// A fixed TCP port.
    Port { host: String, port: u16 },
    /// A TCP port read from an environment variable.
    PortEnv { host: String, var: String },
    /// A fixed Unix domain socket path.
    Socket(String),
    /// A Unix domain socket path read from an environment variable.
    SocketEnv(String),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceRoute {
    pub id: String,
    pub hosts: Vec<String>,
    pub target: RouteTarget,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ServiceDef {
    pub name: String,
    /// `None` marks an externally managed (proxy-only) service.
    pub command: Option<Vec<String>>,
    pub env: BTreeMap<String, String>,
    pub routes: Vec<ServiceRoute>,
    pub disabled: bool,
    /// False keeps the service startable on demand but skips it in start-all.
    pub auto_start: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Manifest {
    pub http_port: u16,
    pub bind: Vec<String>,
    /// Services in declaration order.
    pub services: Vec<ServiceDef>,
}

impl Manifest {
    pub fn service(&self, name: &str) -> Option<&ServiceDef> {
        self.services.iter().find(|service| service.name == name)
    }
}

/// Load and validate a manifest file.
pub fn load_manifest(path: &Path) -> Result<Manifest, ConfigError> {
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
            return Err(ConfigError::new(format!(
                "Services manifest not found: {}",
                path.display()
            )));
        }
        Err(err) => {
            return Err(ConfigError::new(format!(
                "Could not read {}: {err}",
                path.display()
            )));
        }
    };
    parse_manifest(&text)
}

/// Parse and validate raw TOML text without touching the filesystem.
pub fn parse_manifest(text: &str) -> Result<Manifest, ConfigError> {
    let data: Table = text
        .parse()
        .map_err(|err| ConfigError::new(format!("Invalid TOML: {err}")))?;
    build_manifest(&data)
}

fn build_manifest(data: &Table) -> Result<Manifest, ConfigError> {
    reject_unknown_keys(data, TOP_LEVEL_KEYS, "top level")?;
    let http_port = data
        .get("http_port")
        .ok_or_else(|| ConfigError::new("http_port is required"))?;
    let bind = data
        .get("bind")
        .ok_or_else(|| ConfigError::new("bind is required"))?;
    let http_port = parse_port(http_port, "http_port")?;
    let bind = parse_string_list(bind, "bind")?;

    let services_table = match data.get("services") {
        Some(Value::Table(table)) if !table.is_empty() => table,
        _ => {
            return Err(ConfigError::new(
                "[services] must contain at least one service",
            ));
        }
    };

    let services = services_table
        .iter()
        .map(|(name, raw)| parse_service(name, raw))
        .collect::<Result<Vec<_>, _>>()?;

    Ok(Manifest {
        http_port,
        bind,
        services,
    })
}

fn parse_service(name: &str, raw: &Value) -> Result<ServiceDef, ConfigError> {
    let valid_name = !name.is_empty()
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-');
    if !valid_name {
        return Err(ConfigError::new(format!(
            "service name {name:?} may only contain ASCII letters, digits, '_', and '-'"
        )));
    }
    if RESERVED_SERVICE_NAMES.contains(&name) {
        return Err(ConfigError::new(format!(
            "service name {name:?} is reserved"
        )));
    }
    let prefix = format!("services.{name}");
    let Value::Table(raw) = raw else {
        return Err(ConfigError::new(format!("{prefix} must be a table")));
    };
    reject_unknown_keys(raw, SERVICE_KEYS, &prefix)?;

    let command = raw
        .get("command")
        .map(|value| parse_string_list(value, &format!("{prefix}.command")))
        .transpose()?;

    let env = match raw.get("env") {
        Some(value) => parse_string_map(value, &format!("{prefix}.env"))?,
        None => BTreeMap::new(),
    };

    let disabled = parse_bool(raw.get("disabled"), false, &format!("{prefix}.disabled"))?;
    let auto_start = parse_bool(raw.get("auto_start"), true, &format!("{prefix}.auto_start"))?;
    if raw.contains_key("auto_start") && command.is_none() {
        return Err(ConfigError::new(format!(
            "{prefix}.auto_start requires command; there is nothing to start"
        )));
    }

    let routes = match raw.get("routes") {
        None => Vec::new(),
        Some(Value::Array(items)) => items
            .iter()
            .enumerate()
            .map(|(index, item)| parse_route(item, &format!("{prefix}.routes[{index}]")))
            .collect::<Result<Vec<_>, _>>()?,
        Some(_) => {
            return Err(ConfigError::new(format!(
                "{prefix}.routes must be an array"
            )));
        }
    };

    Ok(ServiceDef {
        name: name.to_owned(),
        command,
        env,
        routes,
        disabled,
        auto_start,
    })
}

fn parse_route(raw: &Value, prefix: &str) -> Result<ServiceRoute, ConfigError> {
    let Value::Table(raw) = raw else {
        return Err(ConfigError::new(format!("{prefix} must be a table")));
    };
    reject_unknown_keys(raw, ROUTE_KEYS, prefix)?;

    let id = match raw.get("id") {
        Some(Value::String(id)) if !id.is_empty() => id.clone(),
        _ => {
            return Err(ConfigError::new(format!(
                "{prefix}.id must be a non-empty string"
            )));
        }
    };
    let hosts = parse_string_list(
        raw.get("hosts").unwrap_or(&Value::Array(Vec::new())),
        &format!("{prefix}.hosts"),
    )?;

    let selected: Vec<&str> = TARGET_KEYS
        .iter()
        .copied()
        .filter(|key| raw.contains_key(*key))
        .collect();
    let [selected] = selected.as_slice() else {
        return Err(ConfigError::new(format!(
            "{prefix}: set exactly one of target_port, target_port_env, \
             target_socket, or target_socket_env"
        )));
    };
    let value = &raw[*selected];
    let field = format!("{prefix}.{selected}");

    let target = match *selected {
        "target_port" => RouteTarget::Port {
            host: parse_target_host(raw, prefix)?,
            port: parse_port(value, &field)?,
        },
        "target_port_env" => RouteTarget::PortEnv {
            host: parse_target_host(raw, prefix)?,
            var: parse_non_empty_string(value, &field)?,
        },
        socket_key => {
            if raw.contains_key("target_host") {
                return Err(ConfigError::new(format!(
                    "{prefix}.target_host cannot be used with a Unix socket"
                )));
            }
            let path = parse_non_empty_string(value, &field)?;
            if socket_key == "target_socket" {
                RouteTarget::Socket(path)
            } else {
                RouteTarget::SocketEnv(path)
            }
        }
    };

    Ok(ServiceRoute { id, hosts, target })
}

fn parse_target_host(raw: &Table, prefix: &str) -> Result<String, ConfigError> {
    match raw.get("target_host") {
        None => Ok(DEFAULT_TARGET_HOST.to_owned()),
        Some(Value::String(host)) if ALLOWED_TARGET_HOSTS.contains(&host.as_str()) => {
            Ok(host.clone())
        }
        Some(other) => {
            let got = match other {
                Value::String(host) => format!("{host:?}"),
                other => format!("a {}", other.type_str()),
            };
            Err(ConfigError::new(format!(
                "{prefix}.target_host must be one of {}, got: {got}",
                ALLOWED_TARGET_HOSTS.join(", ")
            )))
        }
    }
}

/// Replace `{VAR}` placeholders in command arguments using `lookup`.
///
/// Only upper-case identifiers (`[A-Z_][A-Z0-9_]*`) form placeholders; any
/// other brace text is left untouched.
pub fn resolve_command(
    command: &[String],
    lookup: impl Fn(&str) -> Option<String>,
) -> Result<Vec<String>, ConfigError> {
    command
        .iter()
        .map(|arg| substitute_placeholders(arg, &lookup))
        .collect()
}

fn substitute_placeholders(
    arg: &str,
    lookup: &impl Fn(&str) -> Option<String>,
) -> Result<String, ConfigError> {
    let mut output = String::with_capacity(arg.len());
    let mut rest = arg;
    while let Some(open) = rest.find('{') {
        output.push_str(&rest[..open]);
        let after = &rest[open + 1..];
        match after.find('}') {
            Some(close) if is_placeholder_name(&after[..close]) => {
                let key = &after[..close];
                let value = lookup(key).ok_or_else(|| {
                    ConfigError::new(format!(
                        "Missing env variable for command placeholder: {key}"
                    ))
                })?;
                output.push_str(&value);
                rest = &after[close + 1..];
            }
            _ => {
                output.push('{');
                rest = after;
            }
        }
    }
    output.push_str(rest);
    Ok(output)
}

fn is_placeholder_name(name: &str) -> bool {
    let mut chars = name.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_uppercase() || c == '_')
        && chars.all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_')
}

/// Parse a port number from an environment value.
pub fn require_port(value: Option<&str>, key: &str) -> Result<u16, ConfigError> {
    let raw =
        value.ok_or_else(|| ConfigError::new(format!("Missing required port variable: {key}")))?;
    let parsed: i64 = raw
        .trim()
        .parse()
        .map_err(|_| ConfigError::new(format!("{key} must be an integer, got: {raw:?}")))?;
    u16::try_from(parsed)
        .ok()
        .filter(|port| *port != 0)
        .ok_or_else(|| ConfigError::new(format!("{key} must be in range 1-65535, got: {parsed}")))
}

/// Parse a Unix socket path from an environment value.
pub fn require_socket_path(value: Option<&str>, key: &str) -> Result<String, ConfigError> {
    match value {
        None => Err(ConfigError::new(format!(
            "Missing required socket variable: {key}"
        ))),
        Some("") => Err(ConfigError::new(format!(
            "{key} must be a non-empty socket path"
        ))),
        Some(path) => Ok(path.to_owned()),
    }
}

fn parse_port(value: &Value, field: &str) -> Result<u16, ConfigError> {
    let Value::Integer(value) = value else {
        return Err(ConfigError::new(format!("{field} must be an integer")));
    };
    u16::try_from(*value)
        .ok()
        .filter(|port| *port != 0)
        .ok_or_else(|| ConfigError::new(format!("{field} must be in range 1-65535")))
}

fn parse_bool(value: Option<&Value>, default: bool, field: &str) -> Result<bool, ConfigError> {
    match value {
        None => Ok(default),
        Some(Value::Boolean(value)) => Ok(*value),
        Some(_) => Err(ConfigError::new(format!("{field} must be a boolean"))),
    }
}

fn parse_non_empty_string(value: &Value, field: &str) -> Result<String, ConfigError> {
    match value {
        Value::String(value) if !value.is_empty() => Ok(value.clone()),
        _ => Err(ConfigError::new(format!(
            "{field} must be a non-empty string"
        ))),
    }
}

fn parse_string_list(value: &Value, field: &str) -> Result<Vec<String>, ConfigError> {
    let error = || ConfigError::new(format!("{field} must be a non-empty list of strings"));
    let Value::Array(items) = value else {
        return Err(error());
    };
    if items.is_empty() {
        return Err(error());
    }
    items
        .iter()
        .map(|item| match item {
            Value::String(item) if !item.is_empty() => Ok(item.clone()),
            _ => Err(error()),
        })
        .collect()
}

fn parse_string_map(value: &Value, field: &str) -> Result<BTreeMap<String, String>, ConfigError> {
    let error = || ConfigError::new(format!("{field} must be a table of string values"));
    let Value::Table(table) = value else {
        return Err(error());
    };
    table
        .iter()
        .map(|(key, item)| match item {
            Value::String(item) => Ok((key.clone(), item.clone())),
            _ => Err(error()),
        })
        .collect()
}

fn reject_unknown_keys(table: &Table, allowed: &[&str], field: &str) -> Result<(), ConfigError> {
    let mut unknown: Vec<&str> = table
        .keys()
        .map(String::as_str)
        .filter(|key| !allowed.contains(key))
        .collect();
    if unknown.is_empty() {
        return Ok(());
    }
    unknown.sort_unstable();
    Err(ConfigError::new(format!(
        "{field} contains unknown keys: {}",
        unknown.join(", ")
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SERVICES_TOML: &str = r#"
http_port = 2800
bind = ["127.0.0.1", "::1"]

[services.minio]
command = ["minio", "server", "data", "--address", ":{MINIO_PORT}"]
env = {MINIO_BROWSER_REDIRECT = "off", MINIO_PORT = "19000", MINIO_CONSOLE_PORT = "19001"}

[[services.minio.routes]]
id = "minio"
hosts = ["minios3.localhost", "*.minios3.localhost"]
target_port_env = "MINIO_PORT"

[[services.minio.routes]]
id = "minioconsole"
hosts = ["minioconsole.localhost"]
target_port_env = "MINIO_CONSOLE_PORT"

[services.s3browser]
command = ["s3browser", "-b", "127.0.0.1:{S3BROWSER_PORT}"]
env = {S3BROWSER_PORT = "18170"}

[[services.s3browser.routes]]
id = "s3browser"
hosts = ["s3browser.localhost"]
target_port_env = "S3BROWSER_PORT"
"#;

    const APP_SERVICE: &str = r#"
[services.app]
command = ["serve"]

[[services.app.routes]]
id = "app"
hosts = ["app.localhost"]
target_port = 3000
"#;

    fn error_of(text: &str) -> String {
        parse_manifest(text).unwrap_err().0
    }

    fn with_header(body: &str) -> String {
        format!("http_port = 2800\nbind = [\"127.0.0.1\"]\n{body}")
    }

    #[test]
    fn manifest_fields() {
        let manifest = parse_manifest(SERVICES_TOML).unwrap();
        assert_eq!(manifest.http_port, 2800);
        assert_eq!(manifest.bind, ["127.0.0.1", "::1"]);
        let names: Vec<_> = manifest.services.iter().map(|s| s.name.as_str()).collect();
        assert_eq!(names, ["minio", "s3browser"]);
    }

    #[test]
    fn service_def_fields() {
        let manifest = parse_manifest(SERVICES_TOML).unwrap();
        let minio = manifest.service("minio").unwrap();
        assert_eq!(minio.env["MINIO_PORT"], "19000");
        assert_eq!(minio.env["MINIO_BROWSER_REDIRECT"], "off");
        let ids: Vec<_> = minio.routes.iter().map(|r| r.id.as_str()).collect();
        assert_eq!(ids, ["minio", "minioconsole"]);
        assert_eq!(
            minio.routes[0].target,
            RouteTarget::PortEnv {
                host: "localhost".into(),
                var: "MINIO_PORT".into()
            }
        );
    }

    #[test]
    fn bundled_sample_covers_supported_service_and_target_forms() {
        let manifest = parse_manifest(crate::SAMPLE_CONFIG).unwrap();
        let get = |name| manifest.service(name).unwrap();

        assert!(get("api").command.is_some());
        assert!(get("frontend").command.is_none());
        assert!(get("external_socket").command.is_none());
        assert!(get("optional_service").disabled);
        assert!(!get("on_demand").auto_start);
        assert!(get("worker").routes.is_empty());

        let socket_app = get("socket_app");
        assert_eq!(socket_app.env["APP_SOCKET"], "run/my-socket-app.sock");
        assert_eq!(
            socket_app.routes[0].target,
            RouteTarget::SocketEnv("APP_SOCKET".into())
        );

        let inherited = get("inherited_environment");
        assert!(inherited.env.is_empty());
        assert!(matches!(
            &inherited.routes[0].target,
            RouteTarget::PortEnv { var, .. } if var == "INHERITED_PORT"
        ));

        let mut forms = std::collections::BTreeSet::new();
        let mut hosts = std::collections::BTreeSet::new();
        let (mut exact, mut wildcard) = (false, false);
        for route in manifest.services.iter().flat_map(|s| &s.routes) {
            match &route.target {
                RouteTarget::Port { host, .. } => {
                    forms.insert("target_port");
                    hosts.insert(host.clone());
                }
                RouteTarget::PortEnv { host, .. } => {
                    forms.insert("target_port_env");
                    hosts.insert(host.clone());
                }
                RouteTarget::Socket(_) => {
                    forms.insert("target_socket");
                }
                RouteTarget::SocketEnv(_) => {
                    forms.insert("target_socket_env");
                }
            }
            exact |= route.hosts.iter().any(|h| !h.contains('*'));
            wildcard |= route.hosts.iter().any(|h| h.contains('*'));
        }
        assert_eq!(
            forms.into_iter().collect::<Vec<_>>(),
            [
                "target_port",
                "target_port_env",
                "target_socket",
                "target_socket_env"
            ]
        );
        assert_eq!(
            hosts.into_iter().collect::<Vec<_>>(),
            ["127.0.0.1", "::1", "localhost"]
        );
        assert!(exact && wildcard);
    }

    #[test]
    fn manifest_requires_proxy_settings() {
        assert_eq!(error_of(APP_SERVICE), "http_port is required");
        assert_eq!(
            error_of(&format!("http_port = 2800\n{APP_SERVICE}")),
            "bind is required"
        );
        assert_eq!(
            error_of("http_port = 2800\nbind = [\"127.0.0.1\"]\n"),
            "[services] must contain at least one service"
        );
    }

    #[test]
    fn manifest_rejects_unknown_and_coerced_fields() {
        assert_eq!(
            error_of(&format!(
                "http_port = \"2800\"\nbind = [\"127.0.0.1\"]\n{APP_SERVICE}"
            )),
            "http_port must be an integer"
        );
        assert_eq!(
            error_of(&format!(
                "http_port = true\nbind = [\"127.0.0.1\"]\n{APP_SERVICE}"
            )),
            "http_port must be an integer"
        );
        assert_eq!(
            error_of(&format!(
                "http_port = 2800\nbind = [\"127.0.0.1\"]\nold_admin_port = 2801\n{APP_SERVICE}"
            )),
            "top level contains unknown keys: old_admin_port"
        );
        assert_eq!(
            error_of(&with_header(
                "[services.app]\ncommand = [\"x\"]\nport = 1\n"
            )),
            "services.app contains unknown keys: port"
        );
        assert!(error_of("http_port = ").starts_with("Invalid TOML: "));
    }

    #[test]
    fn service_names_must_be_safe_and_unreserved() {
        for name in ["\"../escape\"", "\"has space\"", "\"\""] {
            assert!(
                error_of(&with_header(&format!("[services.{name}]\n")))
                    .contains("may only contain"),
                "{name}"
            );
        }
        assert_eq!(
            error_of(&with_header("[services.manager]\n")),
            "service name \"manager\" is reserved"
        );
        assert!(parse_manifest(&with_header("[services.my-app_2]\n")).is_ok());
    }

    #[test]
    fn port_range_is_enforced() {
        assert_eq!(
            error_of(&format!(
                "http_port = 70000\nbind = [\"127.0.0.1\"]\n{APP_SERVICE}"
            )),
            "http_port must be in range 1-65535"
        );
    }

    #[test]
    fn resolve_command_replaces_placeholders() {
        let command: Vec<String> = [
            "minio",
            "server",
            "--address",
            ":{MINIO_PORT}",
            "{lower}",
            "{",
        ]
        .map(String::from)
        .into();
        let resolved = resolve_command(&command, |key| {
            (key == "MINIO_PORT").then(|| "19000".into())
        })
        .unwrap();
        assert_eq!(
            resolved,
            ["minio", "server", "--address", ":19000", "{lower}", "{"]
        );
    }

    #[test]
    fn resolve_command_raises_on_missing_var() {
        let command: Vec<String> = ["s3browser", "-b", "127.0.0.1:{S3BROWSER_PORT}"]
            .map(String::from)
            .into();
        let err = resolve_command(&command, |_| None).unwrap_err();
        assert!(err.0.contains("S3BROWSER_PORT"));
    }

    #[test]
    fn target_port_fixed() {
        let manifest = parse_manifest(&with_header(
            "[services.fixed]\ncommand = [\"fixed-server\"]\n\n[[services.fixed.routes]]\n\
             id = \"fixed\"\nhosts = [\"fixed.localhost\"]\ntarget_port = 3000\n",
        ))
        .unwrap();
        assert_eq!(
            manifest.services[0].routes[0].target,
            RouteTarget::Port {
                host: "localhost".into(),
                port: 3000
            }
        );
    }

    #[test]
    fn unmanaged_service_parses() {
        let manifest = parse_manifest(&with_header(
            "[services.external]\nenv = {APP_PORT = \"3000\"}\n\n[[services.external.routes]]\n\
             id = \"external\"\nhosts = [\"external.localhost\"]\ntarget_port_env = \"APP_PORT\"\n",
        ))
        .unwrap();
        let service = &manifest.services[0];
        assert!(service.command.is_none());
        assert_eq!(service.env["APP_PORT"], "3000");
        assert_eq!(service.routes[0].id, "external");
    }

    #[test]
    fn auto_start_defaults_to_true_and_accepts_false() {
        let manifest = parse_manifest(&with_header(
            "[services.eager]\ncommand = [\"eager\"]\n\n[services.manual]\n\
             command = [\"manual\"]\nauto_start = false\n",
        ))
        .unwrap();
        assert!(manifest.service("eager").unwrap().auto_start);
        assert!(!manifest.service("manual").unwrap().auto_start);
    }

    #[test]
    fn auto_start_rejects_non_boolean_and_commandless_services() {
        assert_eq!(
            error_of(&with_header(
                "[services.app]\ncommand = [\"serve\"]\nauto_start = \"false\"\n"
            )),
            "services.app.auto_start must be a boolean"
        );
        assert!(
            error_of(&with_header(
                "[services.external]\nauto_start = false\n\n[[services.external.routes]]\n\
                 id = \"external\"\nhosts = [\"external.localhost\"]\ntarget_port = 3000\n"
            ))
            .starts_with("services.external.auto_start requires command")
        );
    }

    #[test]
    fn command_empty_list_rejected() {
        assert_eq!(
            error_of(&with_header("[services.bad]\ncommand = []\n")),
            "services.bad.command must be a non-empty list of strings"
        );
    }

    #[test]
    fn target_requires_exactly_one_port_or_socket() {
        for targets in [
            "target_port = 3000\ntarget_port_env = \"BAD_PORT\"",
            "target_port = 3000\ntarget_socket = \"/tmp/bad.sock\"",
            "",
        ] {
            let text = with_header(&format!(
                "[services.bad]\ncommand = [\"bad\"]\n\n[[services.bad.routes]]\n\
                 id = \"bad\"\nhosts = [\"bad.localhost\"]\n{targets}\n"
            ));
            assert!(error_of(&text).contains("set exactly one"), "{targets}");
        }
    }

    #[test]
    fn target_socket_fixed_and_env_parse() {
        let manifest = parse_manifest(&with_header(
            "[services.socket]\ncommand = [\"socket-server\"]\nenv = {APP_SOCKET = \"/tmp/app-env.sock\"}\n\n\
             [[services.socket.routes]]\nid = \"fixed-socket\"\nhosts = [\"fixed.localhost\"]\n\
             target_socket = \"/tmp/app.sock\"\n\n\
             [[services.socket.routes]]\nid = \"env-socket\"\nhosts = [\"env.localhost\"]\n\
             target_socket_env = \"APP_SOCKET\"\n",
        ))
        .unwrap();
        let routes = &manifest.services[0].routes;
        assert_eq!(
            routes[0].target,
            RouteTarget::Socket("/tmp/app.sock".into())
        );
        assert_eq!(
            routes[1].target,
            RouteTarget::SocketEnv("APP_SOCKET".into())
        );
    }

    #[test]
    fn target_socket_rejects_target_host() {
        let text = with_header(
            "[services.bad]\ncommand = [\"bad\"]\n\n[[services.bad.routes]]\nid = \"bad\"\n\
             hosts = [\"bad.localhost\"]\ntarget_host = \"127.0.0.1\"\ntarget_socket = \"/tmp/bad.sock\"\n",
        );
        assert!(error_of(&text).contains("cannot be used with a Unix socket"));
    }

    #[test]
    fn target_host_must_be_loopback() {
        let text = with_header(
            "[services.bad]\n\n[[services.bad.routes]]\nid = \"bad\"\n\
             hosts = [\"bad.localhost\"]\ntarget_host = \"example.com\"\ntarget_port = 1\n",
        );
        assert_eq!(
            error_of(&text),
            "services.bad.routes[0].target_host must be one of 127.0.0.1, ::1, localhost, \
             got: \"example.com\""
        );
    }

    #[test]
    fn require_port_validates_values() {
        assert_eq!(require_port(Some("8080"), "P"), Ok(8080));
        assert!(require_port(None, "P").unwrap_err().0.contains("Missing"));
        assert!(
            require_port(Some("x"), "P")
                .unwrap_err()
                .0
                .contains("integer")
        );
        assert!(
            require_port(Some("0"), "P")
                .unwrap_err()
                .0
                .contains("range")
        );
        assert!(
            require_port(Some("65536"), "P")
                .unwrap_err()
                .0
                .contains("range")
        );
    }
}
