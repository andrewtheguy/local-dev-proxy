//! End-to-end proxy behaviour against real upstream servers.

// tungstenite fixes the handshake callback's error type.
#![allow(clippy::result_large_err)]

use std::convert::Infallible;
use std::net::SocketAddr;
use std::time::Duration;

use bytes::Bytes;
use futures_util::{SinkExt, StreamExt};
use http_body_util::{BodyExt, Full, StreamBody};
use hyper::body::{Frame, Incoming};
use hyper::service::service_fn;
use hyper::{Request, Response, StatusCode};
use hyper_util::rt::TokioIo;
use local_dev_proxy::proxy::ProxyServer;
use local_dev_proxy::routes::{ResolvedRoute, ResolvedTarget, RouteTable};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::oneshot;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use tokio_tungstenite::tungstenite::handshake::server::{
    ErrorResponse, Request as WsRequest, Response as WsResponse,
};
use tokio_tungstenite::tungstenite::protocol::CloseFrame;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::{Error as WsError, Message};

type TestBody = http_body_util::combinators::BoxBody<Bytes, Infallible>;

/// Echo `method=… path=… host=… body=…`; `/stream` sends one chunk, then
/// holds the response open until the test releases it.
async fn echo(
    request: Request<Incoming>,
    release: Option<oneshot::Receiver<()>>,
) -> Response<TestBody> {
    if request.uri().path() == "/stream" {
        let release = release.expect("stream endpoint needs a release channel");
        let chunks =
            futures_util::stream::unfold((0, Some(release)), |(step, release)| async move {
                match step {
                    0 => Some((
                        Ok::<_, Infallible>(Frame::data(Bytes::from("first;"))),
                        (1, release),
                    )),
                    1 => {
                        let _ = release?.await;
                        Some((Ok(Frame::data(Bytes::from("second"))), (2, None)))
                    }
                    _ => None,
                }
            });
        return Response::new(BodyExt::boxed(StreamBody::new(chunks)));
    }
    let method = request.method().clone();
    let path = request.uri().to_string();
    let host = request
        .headers()
        .get("host")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("")
        .to_owned();
    let body = request.into_body().collect().await.unwrap().to_bytes();
    let text = format!(
        "method={method} path={path} host={host} body={}",
        String::from_utf8_lossy(&body)
    );
    Response::new(Full::new(Bytes::from(text)).boxed())
}

async fn serve_http<S>(stream: S, release: Option<oneshot::Receiver<()>>)
where
    S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static,
{
    let release = std::sync::Mutex::new(release);
    let service = service_fn(move |request| {
        let release = release.lock().unwrap().take();
        async move { Ok::<_, Infallible>(echo(request, release).await) }
    });
    let _ = hyper::server::conn::http1::Builder::new()
        .serve_connection(TokioIo::new(stream), service)
        .await;
}

async fn spawn_http_upstream() -> SocketAddr {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            tokio::spawn(serve_http(stream, None));
        }
    });
    addr
}

/// A WebSocket upstream that echoes text as `echo:<text>`, after `check`
/// approves the handshake. Close frames it receives are reported on `closes`.
async fn spawn_ws_upstream(
    check: fn(&WsRequest) -> Result<(), &'static str>,
) -> (
    SocketAddr,
    tokio::sync::mpsc::UnboundedReceiver<Option<(u16, String)>>,
) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let (closes_tx, closes) = tokio::sync::mpsc::unbounded_channel();
    tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let closes_tx = closes_tx.clone();
            tokio::spawn(async move {
                let callback = |request: &WsRequest,
                                response: WsResponse|
                 -> Result<WsResponse, ErrorResponse> {
                    match check(request) {
                        Ok(()) => Ok(response),
                        Err(reason) => Err(Response::builder()
                            .status(StatusCode::FORBIDDEN)
                            .body(Some(reason.to_owned()))
                            .unwrap()),
                    }
                };
                let Ok(mut ws) = tokio_tungstenite::accept_hdr_async(stream, callback).await else {
                    return;
                };
                let _ = ws.send(Message::text("ready")).await;
                while let Some(Ok(message)) = ws.next().await {
                    match message {
                        Message::Text(text) => {
                            let _ = ws.send(Message::text(format!("echo:{text}"))).await;
                        }
                        Message::Close(frame) => {
                            let _ = closes_tx
                                .send(frame.map(|f| (u16::from(f.code), f.reason.to_string())));
                        }
                        _ => {}
                    }
                }
            });
        }
    });
    (addr, closes)
}

fn tcp_route(host: &str, upstream: SocketAddr) -> ResolvedRoute {
    ResolvedRoute {
        id: "test-service".into(),
        host_patterns: vec![host.into()],
        target: ResolvedTarget::Tcp {
            host: "127.0.0.1".into(),
            port: upstream.port(),
        },
    }
}

async fn start_proxy(routes: Vec<ResolvedRoute>) -> (ProxyServer, SocketAddr) {
    let proxy = ProxyServer::bind(RouteTable::new(2800, routes), 0, &["127.0.0.1".into()])
        .await
        .unwrap();
    let addr = proxy.local_addrs()[0];
    (proxy, addr)
}

async fn send(
    proxy: SocketAddr,
    method: &str,
    host: &str,
    path: &str,
    body: &'static str,
) -> Response<Incoming> {
    let stream = TcpStream::connect(proxy).await.unwrap();
    let (mut sender, connection) = hyper::client::conn::http1::handshake(TokioIo::new(stream))
        .await
        .unwrap();
    tokio::spawn(connection);
    let request = Request::builder()
        .method(method)
        .uri(path)
        .header("host", host)
        .body(Full::new(Bytes::from_static(body.as_bytes())))
        .unwrap();
    sender.send_request(request).await.unwrap()
}

async fn text_of(response: Response<Incoming>) -> String {
    let body = response.into_body().collect().await.unwrap().to_bytes();
    String::from_utf8(body.to_vec()).unwrap()
}

async fn ws_connect(
    proxy: SocketAddr,
    host: &str,
    extra_headers: &[(&'static str, &'static str)],
) -> Result<tokio_tungstenite::WebSocketStream<TcpStream>, WsError> {
    let mut request = format!("ws://{host}/ws").into_client_request().unwrap();
    for (name, value) in extra_headers {
        request.headers_mut().insert(*name, value.parse().unwrap());
    }
    let stream = TcpStream::connect(proxy).await.unwrap();
    tokio_tungstenite::client_async(request, stream)
        .await
        .map(|(ws, _response)| ws)
}

async fn next_text(ws: &mut tokio_tungstenite::WebSocketStream<TcpStream>) -> String {
    match tokio::time::timeout(Duration::from_secs(5), ws.next())
        .await
        .unwrap()
    {
        Some(Ok(Message::Text(text))) => text.to_string(),
        other => panic!("expected a text message, got {other:?}"),
    }
}

fn accept_all(_: &WsRequest) -> Result<(), &'static str> {
    Ok(())
}

#[tokio::test]
async fn portal_is_served_on_localhost() {
    let upstream = spawn_http_upstream().await;
    let (_proxy, addr) = start_proxy(vec![tcp_route("test.localhost", upstream)]).await;

    let response = send(addr, "GET", "localhost:2800", "/", "").await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        response.headers()["content-type"],
        "text/html; charset=utf-8"
    );
    let text = text_of(response).await;
    assert!(text.contains("<a href=\"http://test.localhost:2800/\">test.localhost</a>"));
}

#[tokio::test]
async fn http_is_forwarded_with_original_host_path_and_query() {
    let upstream = spawn_http_upstream().await;
    let (_proxy, addr) = start_proxy(vec![tcp_route("test.localhost", upstream)]).await;

    let response = send(
        addr,
        "GET",
        "Test.Localhost:2800",
        "/some/path?x=1&y=%20",
        "",
    )
    .await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        text_of(response).await,
        "method=GET path=/some/path?x=1&y=%20 host=Test.Localhost:2800 body="
    );
}

#[tokio::test]
async fn post_body_is_forwarded() {
    let upstream = spawn_http_upstream().await;
    let (_proxy, addr) = start_proxy(vec![tcp_route("test.localhost", upstream)]).await;

    let response = send(addr, "POST", "test.localhost", "/upload", "hello").await;
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        text_of(response).await,
        "method=POST path=/upload host=test.localhost body=hello"
    );
}

#[tokio::test]
async fn wildcard_routes_match_subdomains() {
    let upstream = spawn_http_upstream().await;
    let (_proxy, addr) = start_proxy(vec![tcp_route("*.test.localhost", upstream)]).await;

    let response = send(addr, "GET", "api.test.localhost", "/", "").await;
    assert_eq!(response.status(), StatusCode::OK);
}

#[tokio::test]
async fn unknown_host_is_404() {
    let (_proxy, addr) = start_proxy(vec![]).await;
    let response = send(addr, "GET", "unknown.localhost", "/", "").await;
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn unreachable_upstream_is_502() {
    let closed = TcpListener::bind("127.0.0.1:0")
        .await
        .unwrap()
        .local_addr()
        .unwrap();
    let (_proxy, addr) = start_proxy(vec![tcp_route("test.localhost", closed)]).await;

    let response = send(addr, "GET", "test.localhost", "/", "").await;
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    assert_eq!(text_of(response).await, "Bad Gateway");
}

#[tokio::test]
async fn responses_stream_before_the_upstream_finishes() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let upstream = listener.local_addr().unwrap();
    let (release_tx, release_rx) = oneshot::channel();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        serve_http(stream, Some(release_rx)).await;
    });
    let (_proxy, addr) = start_proxy(vec![tcp_route("test.localhost", upstream)]).await;

    let response = send(addr, "GET", "test.localhost", "/stream", "").await;
    let mut body = response.into_body();
    let first = tokio::time::timeout(Duration::from_secs(5), body.frame())
        .await
        .expect("first chunk arrived while the upstream was still open")
        .unwrap()
        .unwrap();
    assert_eq!(first.into_data().unwrap(), "first;");

    release_tx.send(()).unwrap();
    let rest = body.collect().await.unwrap().to_bytes();
    assert_eq!(rest, "second");
}

#[tokio::test]
async fn websocket_is_tunnelled() {
    let (upstream, _closes) = spawn_ws_upstream(accept_all).await;
    let (_proxy, addr) = start_proxy(vec![tcp_route("test.localhost", upstream)]).await;

    let mut ws = ws_connect(addr, "test.localhost", &[]).await.unwrap();
    assert_eq!(next_text(&mut ws).await, "ready");
    ws.send(Message::text("hello")).await.unwrap();
    assert_eq!(next_text(&mut ws).await, "echo:hello");
}

#[tokio::test]
async fn websocket_handshake_headers_are_forwarded() {
    fn check(request: &WsRequest) -> Result<(), &'static str> {
        let header = |name| request.headers().get(name).and_then(|v| v.to_str().ok());
        if header("cookie") != Some("session=abc123") {
            return Err("missing cookie");
        }
        if header("origin") != Some("http://test.localhost") {
            return Err("missing origin");
        }
        if header("host") != Some("test.localhost") {
            return Err("wrong host");
        }
        if header("sec-websocket-protocol") != Some("chat") {
            return Err("missing protocol");
        }
        Ok(())
    }
    let (upstream, _closes) = spawn_ws_upstream(check).await;
    let (_proxy, addr) = start_proxy(vec![tcp_route("test.localhost", upstream)]).await;

    // The upstream accepts without echoing the subprotocol, which a strict
    // client rejects; that still proves the handshake headers arrived intact.
    let result = ws_connect(
        addr,
        "test.localhost",
        &[
            ("cookie", "session=abc123"),
            ("origin", "http://test.localhost"),
            ("sec-websocket-protocol", "chat"),
        ],
    )
    .await;
    match result {
        Ok(_) | Err(WsError::Protocol(_)) => {}
        Err(other) => panic!("handshake was rejected: {other:?}"),
    }
    let rejected = ws_connect(
        addr,
        "test.localhost",
        &[("origin", "http://test.localhost")],
    )
    .await;
    match rejected {
        Err(WsError::Http(response)) => {
            assert_eq!(response.status(), StatusCode::FORBIDDEN);
            // tungstenite reads the raw body without undoing the chunked
            // framing the proxy uses for a body of unknown length.
            let body = String::from_utf8_lossy(response.body().as_deref().unwrap_or_default())
                .into_owned();
            assert!(body.contains("missing cookie"), "{body:?}");
        }
        other => panic!("expected the upstream's 403 to be relayed, got {other:?}"),
    }
}

#[tokio::test]
async fn websocket_close_code_and_reason_are_forwarded() {
    let (upstream, mut closes) = spawn_ws_upstream(accept_all).await;
    let (_proxy, addr) = start_proxy(vec![tcp_route("test.localhost", upstream)]).await;

    let mut ws = ws_connect(addr, "test.localhost", &[]).await.unwrap();
    assert_eq!(next_text(&mut ws).await, "ready");
    ws.close(Some(CloseFrame {
        code: CloseCode::from(4001),
        reason: "client shutdown".into(),
    }))
    .await
    .unwrap();
    let echoed = loop {
        match tokio::time::timeout(Duration::from_secs(5), ws.next())
            .await
            .unwrap()
        {
            Some(Ok(Message::Close(frame))) => break frame,
            Some(Ok(_)) => continue,
            other => panic!("expected the upstream's close reply, got {other:?}"),
        }
    };
    assert_eq!(echoed.map(|f| u16::from(f.code)), Some(4001));
    let received = tokio::time::timeout(Duration::from_secs(5), closes.recv())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(received, Some((4001, "client shutdown".into())));
}

#[tokio::test]
async fn websocket_to_unreachable_upstream_is_502() {
    let closed = TcpListener::bind("127.0.0.1:0")
        .await
        .unwrap()
        .local_addr()
        .unwrap();
    let (_proxy, addr) = start_proxy(vec![tcp_route("test.localhost", closed)]).await;

    match ws_connect(addr, "test.localhost", &[]).await {
        Err(WsError::Http(response)) => assert_eq!(response.status(), StatusCode::BAD_GATEWAY),
        other => panic!("expected 502, got {other:?}"),
    }
}

#[tokio::test]
async fn shutdown_releases_the_port_and_closes_tunnels() {
    let (upstream, _closes) = spawn_ws_upstream(accept_all).await;
    let (proxy, addr) = start_proxy(vec![tcp_route("test.localhost", upstream)]).await;
    let mut ws = ws_connect(addr, "test.localhost", &[]).await.unwrap();
    assert_eq!(next_text(&mut ws).await, "ready");

    proxy.shutdown().await;
    let ended = tokio::time::timeout(Duration::from_secs(5), ws.next())
        .await
        .unwrap();
    assert!(!matches!(ended, Some(Ok(Message::Text(_)))));
    TcpListener::bind(addr)
        .await
        .expect("port is free after shutdown");
}

#[cfg(unix)]
mod unix_sockets {
    use super::*;
    use tokio::net::UnixListener;

    fn socket_route(path: &std::path::Path) -> ResolvedRoute {
        ResolvedRoute {
            id: "socket-service".into(),
            host_patterns: vec!["socket.localhost".into()],
            target: ResolvedTarget::Unix(path.to_path_buf()),
        }
    }

    #[tokio::test]
    async fn http_is_forwarded_over_unix_socket() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("app.sock");
        let listener = UnixListener::bind(&path).unwrap();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                tokio::spawn(serve_http(stream, None));
            }
        });
        let (_proxy, addr) = start_proxy(vec![socket_route(&path)]).await;

        let response = send(addr, "GET", "socket.localhost", "/some/path", "").await;
        assert_eq!(response.status(), StatusCode::OK);
        assert!(
            text_of(response)
                .await
                .starts_with("method=GET path=/some/path host=socket.localhost")
        );
    }

    #[tokio::test]
    async fn websocket_is_tunnelled_over_unix_socket() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("ws.sock");
        let listener = UnixListener::bind(&path).unwrap();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                tokio::spawn(async move {
                    let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
                    while let Some(Ok(Message::Text(text))) = ws.next().await {
                        ws.send(Message::text(format!("echo:{text}")))
                            .await
                            .unwrap();
                    }
                });
            }
        });
        let (_proxy, addr) = start_proxy(vec![socket_route(&path)]).await;

        let mut ws = ws_connect(addr, "socket.localhost", &[]).await.unwrap();
        ws.send(Message::text("hello")).await.unwrap();
        assert_eq!(next_text(&mut ws).await, "echo:hello");
    }

    #[tokio::test]
    async fn missing_socket_is_502() {
        let dir = tempfile::tempdir().unwrap();
        let (_proxy, addr) = start_proxy(vec![socket_route(&dir.path().join("none.sock"))]).await;
        let response = send(addr, "GET", "socket.localhost", "/", "").await;
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    }
}
