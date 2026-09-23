//! Host-routed HTTP reverse proxy with WebSocket tunnelling.
//!
//! Requests are matched on their `Host` header. `localhost` serves a portal
//! page linking every route; unknown hosts get 404; unreachable upstreams get
//! 502. Bodies stream in both directions, so long polls and server-sent events
//! work, and WebSocket upgrades are tunnelled byte-for-byte once the upstream
//! accepts the handshake.

use std::convert::Infallible;
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::combinators::BoxBody;
use http_body_util::{BodyExt, Empty, Full};
use hyper::body::Incoming;
use hyper::header::{self, HeaderMap, HeaderValue};
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode, Uri};
use hyper_util::client::legacy::Client;
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::rt::{TokioExecutor, TokioIo};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::watch;
use tokio::task::JoinHandle;

use crate::routes::{ResolvedRoute, ResolvedTarget, RouteTable};

pub type ProxyBody = BoxBody<Bytes, hyper::Error>;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
const LISTEN_BACKLOG: i32 = 1024;

/// Headers that describe a single connection and must not be forwarded.
const HOP_BY_HOP: &[&str] = &[
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
];

#[derive(Debug, thiserror::Error)]
pub enum ProxyError {
    #[error("Could not resolve bind address {host}: {source}")]
    Resolve { host: String, source: io::Error },
    #[error("Bind address {0} resolved to no addresses")]
    NoAddress(String),
    #[error("Could not listen on {addr}: {source}")]
    Listen { addr: SocketAddr, source: io::Error },
}

/// A running proxy listening on one or more addresses.
#[derive(Debug)]
pub struct ProxyServer {
    shutdown: watch::Sender<bool>,
    accept_tasks: Vec<JoinHandle<()>>,
    local_addrs: Vec<SocketAddr>,
}

impl ProxyServer {
    /// Bind every `bind` host at `http_port` and start serving `routes` on the
    /// current Tokio runtime. Fails without serving if any address fails.
    pub async fn bind(
        routes: RouteTable,
        http_port: u16,
        bind: &[String],
    ) -> Result<Self, ProxyError> {
        let mut listeners = Vec::new();
        for host in bind {
            for addr in resolve_bind_host(host, http_port).await? {
                let listener =
                    listen(addr).map_err(|source| ProxyError::Listen { addr, source })?;
                listeners.push(listener);
            }
        }

        let (shutdown, shutdown_rx) = watch::channel(false);
        let proxy = Arc::new(Proxy::new(routes, shutdown_rx.clone()));
        let mut local_addrs = Vec::new();
        let mut accept_tasks = Vec::new();
        for listener in listeners {
            let addr = listener.local_addr().map_err(|source| ProxyError::Listen {
                addr: SocketAddr::from(([0, 0, 0, 0], http_port)),
                source,
            })?;
            tracing::info!("Proxy listening on {addr}");
            local_addrs.push(addr);
            accept_tasks.push(tokio::spawn(accept_loop(
                listener,
                Arc::clone(&proxy),
                shutdown_rx.clone(),
            )));
        }
        Ok(Self {
            shutdown,
            accept_tasks,
            local_addrs,
        })
    }

    pub fn local_addrs(&self) -> &[SocketAddr] {
        &self.local_addrs
    }

    /// Close the listeners and drop every open connection and tunnel.
    pub async fn shutdown(self) {
        let _ = self.shutdown.send(true);
        for task in self.accept_tasks {
            let _ = task.await;
        }
    }
}

async fn resolve_bind_host(host: &str, port: u16) -> Result<Vec<SocketAddr>, ProxyError> {
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok(vec![SocketAddr::new(ip, port)]);
    }
    let mut addrs: Vec<SocketAddr> = tokio::net::lookup_host((host, port))
        .await
        .map_err(|source| ProxyError::Resolve {
            host: host.to_owned(),
            source,
        })?
        .collect();
    addrs.dedup();
    if addrs.is_empty() {
        return Err(ProxyError::NoAddress(host.to_owned()));
    }
    Ok(addrs)
}

fn listen(addr: SocketAddr) -> io::Result<TcpListener> {
    use socket2::{Domain, Protocol, Socket, Type};

    let socket = Socket::new(Domain::for_address(addr), Type::STREAM, Some(Protocol::TCP))?;
    // IPv4 and IPv6 listeners are separate entries in `bind`; never let an
    // IPv6 wildcard also claim the IPv4 port.
    if addr.is_ipv6() {
        socket.set_only_v6(true)?;
    }
    // Allow an immediate rebind after a restart. On Windows this option would
    // instead let another process steal the port, so it is Unix-only.
    #[cfg(unix)]
    socket.set_reuse_address(true)?;
    socket.bind(&addr.into())?;
    socket.listen(LISTEN_BACKLOG)?;
    socket.set_nonblocking(true)?;
    TcpListener::from_std(socket.into())
}

async fn accept_loop(listener: TcpListener, proxy: Arc<Proxy>, shutdown: watch::Receiver<bool>) {
    let mut stop = shutdown.clone();
    loop {
        tokio::select! {
            _ = stopped(&mut stop) => break,
            accepted = listener.accept() => match accepted {
                Ok((stream, _peer)) => {
                    let _ = stream.set_nodelay(true);
                    tokio::spawn(serve_connection(stream, Arc::clone(&proxy), shutdown.clone()));
                }
                Err(err) => {
                    // Typically descriptor exhaustion; back off instead of spinning.
                    tracing::warn!("Proxy accept failed: {err}");
                    tokio::time::sleep(Duration::from_millis(50)).await;
                }
            },
        }
    }
}

/// Resolve once shutdown has been requested (or its sender is gone).
async fn stopped(shutdown: &mut watch::Receiver<bool>) {
    let _ = shutdown.wait_for(|stop| *stop).await;
}

async fn serve_connection(
    stream: TcpStream,
    proxy: Arc<Proxy>,
    mut shutdown: watch::Receiver<bool>,
) {
    let service = service_fn(move |request| {
        let proxy = Arc::clone(&proxy);
        async move { Ok::<_, Infallible>(proxy.handle(request).await) }
    });
    let connection = hyper::server::conn::http1::Builder::new()
        .serve_connection(TokioIo::new(stream), service)
        .with_upgrades();
    tokio::select! {
        result = connection => {
            if let Err(err) = result {
                tracing::debug!("Proxy connection ended with error: {err}");
            }
        }
        _ = stopped(&mut shutdown) => {}
    }
}

#[derive(Debug)]
struct Proxy {
    routes: RouteTable,
    /// Shared keep-alive pool for every TCP upstream.
    tcp: Client<HttpConnector, Incoming>,
    /// One pooled client per Unix socket path, created on first use.
    #[cfg(unix)]
    unix: std::sync::Mutex<
        std::collections::HashMap<std::path::PathBuf, Client<unix::UnixConnector, Incoming>>,
    >,
    shutdown: watch::Receiver<bool>,
}

impl Proxy {
    fn new(routes: RouteTable, shutdown: watch::Receiver<bool>) -> Self {
        let mut connector = HttpConnector::new();
        connector.set_connect_timeout(Some(CONNECT_TIMEOUT));
        connector.set_nodelay(true);
        let tcp = Client::builder(TokioExecutor::new()).build(connector);
        Self {
            routes,
            tcp,
            #[cfg(unix)]
            unix: Default::default(),
            shutdown,
        }
    }

    async fn handle(&self, request: Request<Incoming>) -> Response<ProxyBody> {
        let host = request_host(&request);
        let method = request.method().clone();
        let path = request.uri().path().to_owned();

        if host.eq_ignore_ascii_case("localhost") {
            tracing::info!("{method} {host} {path} -> portal");
            return text_response(
                StatusCode::OK,
                "text/html; charset=utf-8",
                self.routes.portal_html().to_owned(),
            );
        }

        let Some(route) = self.routes.find(&host) else {
            tracing::info!("{method} {host} {path} -> 404");
            return text_response(
                StatusCode::NOT_FOUND,
                "text/plain; charset=utf-8",
                "Not Found",
            );
        };

        if is_websocket_upgrade(request.headers()) {
            tracing::info!("{method} {host} {path} -> ws {}", route.id);
            return self.proxy_websocket(request, route).await;
        }

        let response = self.proxy_http(request, route).await;
        tracing::info!(
            "{method} {host} {path} -> {} {}",
            route.id,
            response.status().as_u16()
        );
        response
    }

    async fn proxy_http(
        &self,
        request: Request<Incoming>,
        route: &ResolvedRoute,
    ) -> Response<ProxyBody> {
        let (mut parts, body) = request.into_parts();
        let Ok(uri) = upstream_uri(&route.target, &parts.uri) else {
            return bad_gateway();
        };
        parts.uri = uri;
        strip_hop_by_hop(&mut parts.headers);
        let upstream_request = Request::from_parts(parts, body);

        let result = match &route.target {
            ResolvedTarget::Tcp { .. } => self.tcp.request(upstream_request).await,
            #[cfg(unix)]
            ResolvedTarget::Unix(path) => self.unix_client(path).request(upstream_request).await,
            #[cfg(not(unix))]
            ResolvedTarget::Unix(_) => {
                tracing::warn!(
                    "Unix socket route {} is not supported on this platform",
                    route.id
                );
                return bad_gateway();
            }
        };
        match result {
            Ok(response) => {
                // A failure after this point surfaces as a body error, which
                // makes hyper abort the client connection rather than end a
                // truncated response cleanly.
                let (mut parts, body) = response.into_parts();
                strip_hop_by_hop(&mut parts.headers);
                Response::from_parts(parts, body.boxed())
            }
            Err(err) => {
                tracing::debug!("Upstream error for route {}: {err}", route.id);
                bad_gateway()
            }
        }
    }

    #[cfg(unix)]
    fn unix_client(&self, path: &std::path::Path) -> Client<unix::UnixConnector, Incoming> {
        let mut clients = self
            .unix
            .lock()
            .unwrap_or_else(|poison| poison.into_inner());
        clients
            .entry(path.to_path_buf())
            .or_insert_with(|| {
                Client::builder(TokioExecutor::new()).build(unix::UnixConnector::new(path))
            })
            .clone()
    }

    async fn proxy_websocket(
        &self,
        mut request: Request<Incoming>,
        route: &ResolvedRoute,
    ) -> Response<ProxyBody> {
        let client_upgrade = hyper::upgrade::on(&mut request);
        let (mut parts, _body) = request.into_parts();
        parts.uri = parts
            .uri
            .path_and_query()
            .map_or_else(|| Uri::from_static("/"), |pq| Uri::from(pq.clone()));
        strip_hop_by_hop(&mut parts.headers);
        parts
            .headers
            .insert(header::CONNECTION, HeaderValue::from_static("upgrade"));
        parts
            .headers
            .insert(header::UPGRADE, HeaderValue::from_static("websocket"));
        if !parts.headers.contains_key(header::HOST)
            && let Ok(value) = HeaderValue::from_str(&target_authority(&route.target))
        {
            parts.headers.insert(header::HOST, value);
        }
        let upstream_request = Request::from_parts(parts, Empty::<Bytes>::new());

        let mut response = match open_upgrade(&route.target, upstream_request).await {
            Ok(response) => response,
            Err(err) => {
                tracing::debug!("WebSocket upstream error for route {}: {err}", route.id);
                return bad_gateway();
            }
        };

        if response.status() != StatusCode::SWITCHING_PROTOCOLS {
            // The upstream rejected the handshake; relay its answer as-is.
            let (mut parts, body) = response.into_parts();
            strip_hop_by_hop(&mut parts.headers);
            return Response::from_parts(parts, body.boxed());
        }

        let upstream_upgrade = hyper::upgrade::on(&mut response);
        let mut shutdown = self.shutdown.clone();
        let route_id = route.id.clone();
        tokio::spawn(async move {
            let (client, upstream) = match tokio::try_join!(client_upgrade, upstream_upgrade) {
                Ok(pair) => pair,
                Err(err) => {
                    tracing::warn!("WebSocket upgrade for route {route_id} failed: {err}");
                    return;
                }
            };
            let mut client = TokioIo::new(client);
            let mut upstream = TokioIo::new(upstream);
            tokio::select! {
                result = tokio::io::copy_bidirectional(&mut client, &mut upstream) => {
                    if let Err(err) = result {
                        tracing::debug!("WebSocket tunnel for route {route_id} ended: {err}");
                    }
                }
                _ = stopped(&mut shutdown) => {}
            }
        });

        let (parts, _body) = response.into_parts();
        Response::from_parts(parts, empty_body())
    }
}

/// Send `request` over a dedicated upstream connection that supports upgrades.
async fn open_upgrade(
    target: &ResolvedTarget,
    request: Request<Empty<Bytes>>,
) -> Result<Response<Incoming>, Box<dyn std::error::Error + Send + Sync>> {
    match target {
        ResolvedTarget::Tcp { host, port } => {
            let stream =
                tokio::time::timeout(CONNECT_TIMEOUT, TcpStream::connect((host.as_str(), *port)))
                    .await??;
            let _ = stream.set_nodelay(true);
            send_upgrade_request(TokioIo::new(stream), request).await
        }
        #[cfg(unix)]
        ResolvedTarget::Unix(path) => {
            let stream =
                tokio::time::timeout(CONNECT_TIMEOUT, tokio::net::UnixStream::connect(path))
                    .await??;
            send_upgrade_request(TokioIo::new(stream), request).await
        }
        #[cfg(not(unix))]
        ResolvedTarget::Unix(_) => {
            Err("Unix socket targets are not supported on this platform".into())
        }
    }
}

async fn send_upgrade_request<T>(
    io: TokioIo<T>,
    request: Request<Empty<Bytes>>,
) -> Result<Response<Incoming>, Box<dyn std::error::Error + Send + Sync>>
where
    T: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let (mut sender, connection) = hyper::client::conn::http1::handshake(io).await?;
    tokio::spawn(async move {
        if let Err(err) = connection.with_upgrades().await {
            tracing::debug!("WebSocket upstream connection ended with error: {err}");
        }
    });
    Ok(sender.send_request(request).await?)
}

/// The request's host name, without any port.
fn request_host(request: &Request<Incoming>) -> String {
    let raw = request
        .headers()
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
        .or_else(|| {
            request
                .uri()
                .authority()
                .map(|authority| authority.as_str())
        })
        .unwrap_or("");
    strip_port(raw).to_owned()
}

fn strip_port(raw: &str) -> &str {
    if raw.starts_with('[') {
        return raw.find(']').map_or(raw, |end| &raw[..=end]);
    }
    raw.rsplit_once(':').map_or(raw, |(host, _port)| host)
}

fn is_websocket_upgrade(headers: &HeaderMap) -> bool {
    headers
        .get(header::UPGRADE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.eq_ignore_ascii_case("websocket"))
}

fn strip_hop_by_hop(headers: &mut HeaderMap) {
    for name in HOP_BY_HOP {
        headers.remove(*name);
    }
}

fn target_authority(target: &ResolvedTarget) -> String {
    match target {
        ResolvedTarget::Tcp { host, port } if host.contains(':') => format!("[{host}]:{port}"),
        ResolvedTarget::Tcp { host, port } => format!("{host}:{port}"),
        ResolvedTarget::Unix(_) => "localhost".to_owned(),
    }
}

fn upstream_uri(
    target: &ResolvedTarget,
    original: &Uri,
) -> Result<Uri, hyper::http::uri::InvalidUri> {
    let path_and_query = original.path_and_query().map_or("/", |pq| pq.as_str());
    format!("http://{}{path_and_query}", target_authority(target)).parse()
}

fn empty_body() -> ProxyBody {
    Empty::<Bytes>::new()
        .map_err(|never| match never {})
        .boxed()
}

fn text_response(
    status: StatusCode,
    content_type: &'static str,
    body: impl Into<Bytes>,
) -> Response<ProxyBody> {
    let mut response = Response::new(
        Full::new(body.into())
            .map_err(|never| match never {})
            .boxed(),
    );
    *response.status_mut() = status;
    response
        .headers_mut()
        .insert(header::CONTENT_TYPE, HeaderValue::from_static(content_type));
    response
}

fn bad_gateway() -> Response<ProxyBody> {
    text_response(
        StatusCode::BAD_GATEWAY,
        "text/plain; charset=utf-8",
        "Bad Gateway",
    )
}

#[cfg(unix)]
mod unix {
    use std::future::Future;
    use std::io;
    use std::path::{Path, PathBuf};
    use std::pin::Pin;
    use std::sync::Arc;
    use std::task::{Context, Poll};

    use hyper::Uri;
    use hyper_util::rt::TokioIo;
    use tokio::net::UnixStream;

    use super::CONNECT_TIMEOUT;

    /// Connects every request to one Unix domain socket, ignoring the URI.
    #[derive(Debug, Clone)]
    pub struct UnixConnector(Arc<PathBuf>);

    impl UnixConnector {
        pub fn new(path: &Path) -> Self {
            Self(Arc::new(path.to_path_buf()))
        }
    }

    impl tower_service::Service<Uri> for UnixConnector {
        type Response = TokioIo<UnixStream>;
        type Error = io::Error;
        type Future = Pin<Box<dyn Future<Output = io::Result<Self::Response>> + Send>>;

        fn poll_ready(&mut self, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }

        fn call(&mut self, _uri: Uri) -> Self::Future {
            let path = Arc::clone(&self.0);
            Box::pin(async move {
                let stream = tokio::time::timeout(CONNECT_TIMEOUT, UnixStream::connect(&*path))
                    .await
                    .map_err(|_| {
                        io::Error::new(io::ErrorKind::TimedOut, "Unix socket connect timed out")
                    })??;
                Ok(TokioIo::new(stream))
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strip_port_handles_names_and_ipv6() {
        assert_eq!(strip_port("app.localhost:2800"), "app.localhost");
        assert_eq!(strip_port("app.localhost"), "app.localhost");
        assert_eq!(strip_port("[::1]:2800"), "[::1]");
        assert_eq!(strip_port("[::1]"), "[::1]");
    }

    #[test]
    fn upstream_uri_keeps_path_and_query() {
        let original: Uri = "/a/b?c=1".parse().unwrap();
        let tcp = ResolvedTarget::Tcp {
            host: "::1".into(),
            port: 9000,
        };
        assert_eq!(
            upstream_uri(&tcp, &original).unwrap(),
            "http://[::1]:9000/a/b?c=1"
        );
        let unix = ResolvedTarget::Unix("/tmp/x.sock".into());
        assert_eq!(
            upstream_uri(&unix, &original).unwrap(),
            "http://localhost/a/b?c=1"
        );
    }
}
