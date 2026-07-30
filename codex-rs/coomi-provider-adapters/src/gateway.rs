use crate::AdapterError;
use crate::CanonicalEventStream;
use crate::HttpProviderTransport;
use crate::ResponseEvent;
use crate::ResponsesApiRequest;
use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use axum::response::sse::Event;
use axum::response::sse::KeepAlive;
use axum::response::sse::Sse;
use axum::routing::get;
use axum::routing::post;
use futures::Stream;
use futures::StreamExt;
use serde_json::Value;
use serde_json::json;
use std::convert::Infallible;
use std::pin::Pin;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::time::Duration;
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;
use tracing::debug;
use tracing::warn;
use url::Url;

#[derive(Debug)]
pub struct EmbeddedProviderGateway {
    base_url: Url,
    diagnostics: Arc<GatewayDiagnostics>,
    shutdown: Option<oneshot::Sender<()>>,
    task: Option<JoinHandle<()>>,
}

#[derive(Debug)]
struct GatewayState {
    transport: HttpProviderTransport,
    diagnostics: Arc<GatewayDiagnostics>,
}

#[derive(Debug, Default)]
struct GatewayDiagnostics {
    request_count: AtomicUsize,
    last_error: Mutex<Option<String>>,
}

impl GatewayDiagnostics {
    fn record_error(&self, error: impl Into<String>) {
        if let Ok(mut last_error) = self.last_error.lock() {
            *last_error = Some(error.into());
        }
    }
}

type GatewayStream = Pin<Box<dyn Stream<Item = Result<Event, Infallible>> + Send + 'static>>;

impl EmbeddedProviderGateway {
    pub async fn start(transport: HttpProviderTransport) -> Result<Self, AdapterError> {
        let listener = TcpListener::bind("127.0.0.1:0").await.map_err(|error| {
            AdapterError::Config(format!("failed to bind provider gateway: {error}"))
        })?;
        let address = listener.local_addr().map_err(|error| {
            AdapterError::Config(format!(
                "failed to resolve provider gateway address: {error}"
            ))
        })?;
        let base_url = Url::parse(&format!("http://{address}/v1")).map_err(|error| {
            AdapterError::Config(format!("failed to build provider gateway URL: {error}"))
        })?;
        let diagnostics = Arc::new(GatewayDiagnostics::default());
        let state = Arc::new(GatewayState {
            transport,
            diagnostics: Arc::clone(&diagnostics),
        });
        let app = Router::new()
            .route("/v1/responses", post(responses))
            .route("/v1/models", get(models))
            .with_state(state);
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let task = tokio::spawn(async move {
            let result = axum::serve(listener, app)
                .with_graceful_shutdown(async move {
                    let _ = shutdown_rx.await;
                })
                .await;
            if let Err(error) = result {
                warn!(%error, "embedded provider gateway stopped unexpectedly");
            }
        });

        Ok(Self {
            base_url,
            diagnostics,
            shutdown: Some(shutdown_tx),
            task: Some(task),
        })
    }

    pub fn base_url(&self) -> &Url {
        &self.base_url
    }

    pub fn request_count(&self) -> usize {
        self.diagnostics.request_count.load(Ordering::Relaxed)
    }

    pub fn last_error(&self) -> Option<String> {
        self.diagnostics
            .last_error
            .lock()
            .ok()
            .and_then(|error| error.clone())
    }

    pub async fn shutdown(mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
        if let Some(task) = self.task.take() {
            let _ = task.await;
        }
    }
}

impl Drop for EmbeddedProviderGateway {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}

async fn models() -> Json<Value> {
    Json(json!({"object": "list", "data": []}))
}

async fn responses(State(state): State<Arc<GatewayState>>, body: Bytes) -> Response {
    state
        .diagnostics
        .request_count
        .fetch_add(1, Ordering::Relaxed);
    let request = match serde_json::from_slice::<ResponsesApiRequest>(&body) {
        Ok(request) => request,
        Err(error) => {
            let message = format!("invalid canonical Responses request: {error}");
            state.diagnostics.record_error(message.clone());
            return (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": {"type": "invalid_request", "message": message}})),
            )
                .into_response();
        }
    };
    match state.transport.stream_owned(request).await {
        Ok(stream) => Sse::new(gateway_stream(stream, Arc::clone(&state.diagnostics)))
            .keep_alive(
                KeepAlive::new()
                    .interval(Duration::from_secs(15))
                    .text("coomi-provider-gateway"),
            )
            .into_response(),
        Err(error) => {
            state.diagnostics.record_error(error.to_string());
            (
                StatusCode::BAD_GATEWAY,
                Json(json!({
                    "error": {
                        "type": "coomi_provider_error",
                        "message": error.to_string(),
                    }
                })),
            )
                .into_response()
        }
    }
}

fn gateway_stream(
    mut upstream: CanonicalEventStream<'static, AdapterError>,
    diagnostics: Arc<GatewayDiagnostics>,
) -> GatewayStream {
    Box::pin(async_stream::stream! {
        while let Some(event) = upstream.next().await {
            match event {
                Ok(event) => match canonical_sse_event(event) {
                    Ok(Some(event)) => yield Ok(event),
                    Ok(None) => {}
                    Err(error) => {
                        diagnostics.record_error(error.to_string());
                        yield Ok(failed_sse_event(&error));
                        break;
                    }
                },
                Err(error) => {
                    diagnostics.record_error(error.to_string());
                    yield Ok(failed_sse_event(&error));
                    break;
                }
            }
        }
    })
}

fn canonical_sse_event(event: ResponseEvent) -> Result<Option<Event>, AdapterError> {
    let encoded = match event {
        ResponseEvent::Created => Some((
            "response.created",
            json!({
                "type": "response.created",
                "response": {"id": "coomi-provider-gateway"}
            }),
        )),
        ResponseEvent::OutputItemDone(item) => Some((
            "response.output_item.done",
            json!({
                "type": "response.output_item.done",
                "item": serde_json::to_value(item)?,
            }),
        )),
        ResponseEvent::OutputItemAdded(item) => Some((
            "response.output_item.added",
            json!({
                "type": "response.output_item.added",
                "item": serde_json::to_value(item)?,
            }),
        )),
        ResponseEvent::Completed {
            response_id,
            token_usage,
            end_turn,
        } => Some((
            "response.completed",
            json!({
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "usage": token_usage.map(|usage| json!({
                        "input_tokens": usage.input_tokens,
                        "input_tokens_details": {
                            "cached_tokens": usage.cached_input_tokens,
                            "cache_write_tokens": usage.cache_write_input_tokens,
                        },
                        "output_tokens": usage.output_tokens,
                        "output_tokens_details": {
                            "reasoning_tokens": usage.reasoning_output_tokens,
                        },
                        "total_tokens": usage.total_tokens,
                    })),
                    "end_turn": end_turn,
                }
            }),
        )),
        ResponseEvent::OutputTextDelta(delta) => Some((
            "response.output_text.delta",
            json!({"type": "response.output_text.delta", "delta": delta}),
        )),
        ResponseEvent::ToolCallInputDelta {
            item_id,
            call_id,
            delta,
        } => Some((
            "response.custom_tool_call_input.delta",
            json!({
                "type": "response.custom_tool_call_input.delta",
                "item_id": item_id,
                "call_id": call_id,
                "delta": delta,
            }),
        )),
        ResponseEvent::ReasoningSummaryDelta {
            delta,
            summary_index,
        } => Some((
            "response.reasoning_summary_text.delta",
            json!({
                "type": "response.reasoning_summary_text.delta",
                "delta": delta,
                "summary_index": summary_index,
            }),
        )),
        ResponseEvent::ReasoningSummaryDone {
            item_id,
            text,
            summary_index,
        } => Some((
            "response.reasoning_summary_text.done",
            json!({
                "type": "response.reasoning_summary_text.done",
                "item_id": item_id,
                "text": text,
                "summary_index": summary_index,
            }),
        )),
        ResponseEvent::ReasoningContentDelta {
            delta,
            content_index,
        } => Some((
            "response.reasoning_text.delta",
            json!({
                "type": "response.reasoning_text.delta",
                "delta": delta,
                "content_index": content_index,
            }),
        )),
        ResponseEvent::ReasoningSummaryPartAdded { summary_index } => Some((
            "response.reasoning_summary_part.added",
            json!({
                "type": "response.reasoning_summary_part.added",
                "summary_index": summary_index,
            }),
        )),
        ResponseEvent::SafetyBuffering(_)
        | ResponseEvent::ServerModel(_)
        | ResponseEvent::ModelVerifications(_)
        | ResponseEvent::TurnModerationMetadata(_)
        | ResponseEvent::ServerReasoningIncluded(_)
        | ResponseEvent::RateLimits(_)
        | ResponseEvent::ModelsEtag(_) => {
            debug!("provider gateway omitted a response metadata event");
            None
        }
    };

    Ok(encoded.map(|(kind, data)| Event::default().event(kind).data(data.to_string())))
}

fn failed_sse_event(error: &AdapterError) -> Event {
    let data = json!({
        "type": "response.failed",
        "response": {
            "id": "coomi-provider-gateway-error",
            "error": {
                "type": "coomi_provider_error",
                "code": "coomi_provider_error",
                "message": error.to_string(),
            }
        }
    });
    Event::default()
        .event("response.failed")
        .data(data.to_string())
}

#[cfg(test)]
mod tests {
    use super::canonical_sse_event;
    use crate::ResponseEvent;
    use codex_protocol::protocol::TokenUsage;

    #[test]
    fn completed_event_preserves_usage_for_codex_core() {
        let event = canonical_sse_event(ResponseEvent::Completed {
            response_id: "response-1".to_string(),
            token_usage: Some(TokenUsage {
                input_tokens: 11,
                cached_input_tokens: 5,
                cache_write_input_tokens: 2,
                output_tokens: 3,
                reasoning_output_tokens: 1,
                total_tokens: 14,
            }),
            end_turn: Some(true),
        })
        .expect("encode completed event")
        .expect("completed event is emitted");

        let debug = format!("{event:?}");
        assert!(debug.contains("response.completed"));
        assert!(debug.contains("cached_tokens"));
        assert!(debug.contains("response-1"));
    }
}
