//! Provider compatibility boundary for Coomi.
//!
//! Codex owns the canonical request, response, and tool types. Adapters may
//! translate provider wire formats, but must emit these exact canonical events.

mod anthropic;
mod common;
mod config;
mod conformance;
mod error;
mod gemini;
mod http_transport;
mod openai_compatible;
mod responses;

use futures::Stream;
use serde_json::Value;
use std::error::Error;
use std::future::Future;
use std::pin::Pin;

pub use anthropic::AnthropicDecodeState;
pub use anthropic::AnthropicMessagesAdapter;
pub use config::ProviderConfig;
pub use config::ProviderRegistry;
pub use config::SecretString;
pub use conformance::ConformanceCase;
pub use conformance::ConformanceReport;
pub use conformance::ConformanceResult;
pub use conformance::ConformanceStatus;
pub use conformance::run_basic_conformance;
pub use conformance::run_full_conformance;
pub use error::AdapterError;
pub use gemini::GeminiDecodeState;
pub use gemini::GeminiNativeAdapter;
pub use http_transport::HttpProviderTransport;
pub use http_transport::ProviderAdapter;
pub use openai_compatible::OpenAiCompatibleAdapter;
pub use openai_compatible::OpenAiCompatibleDecodeState;
pub use responses::OpenAiResponsesAdapter;
pub use responses::ResponsesDecodeState;

pub use codex_api::ResponseEvent;
pub use codex_api::ResponsesApiRequest;
pub use codex_protocol::models::ResponseItem;
pub use codex_tools::ToolSpec;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CompatibilityGrade {
    Native,
    Compatible,
    Experimental,
    Unsupported,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, serde::Serialize)]
pub enum WireProtocol {
    OpenAiResponses,
    OpenAiCompatible,
    AnthropicMessages,
    GeminiNative,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProviderCapabilities {
    pub wire_protocol: WireProtocol,
    pub streaming: bool,
    pub native_tool_calls: bool,
    pub parallel_tool_calls: bool,
    pub strict_json_schema: bool,
    pub reasoning_summary: bool,
    pub response_continuity: bool,
}

impl ProviderCapabilities {
    pub const fn native_responses() -> Self {
        Self {
            wire_protocol: WireProtocol::OpenAiResponses,
            streaming: true,
            native_tool_calls: true,
            parallel_tool_calls: true,
            strict_json_schema: true,
            reasoning_summary: true,
            response_continuity: true,
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct EncodedRequest {
    pub path: String,
    pub body: Value,
}

#[derive(Clone, Debug, PartialEq)]
pub enum WireEvent {
    Json {
        event_type: Option<String>,
        data: Value,
    },
    Done,
}

/// Pure wire conversion contract used by the embedded provider gateway.
///
/// Implementations never execute tools. They encode a canonical Codex request
/// and decode provider events back into canonical Codex response events.
pub trait WireAdapter: std::fmt::Debug + Send + Sync {
    type DecodeState: Default + Send;

    fn protocol(&self) -> WireProtocol;

    fn encode_request(&self, request: &ResponsesApiRequest)
    -> Result<EncodedRequest, AdapterError>;

    fn decode_event(
        &self,
        state: &mut Self::DecodeState,
        event: WireEvent,
    ) -> Result<Vec<ResponseEvent>, AdapterError>;
}

pub type CanonicalEventStream<'a, E> =
    Pin<Box<dyn Stream<Item = Result<ResponseEvent, E>> + Send + 'a>>;
pub type TransportFuture<'a, E> =
    Pin<Box<dyn Future<Output = Result<CanonicalEventStream<'a, E>, E>> + Send + 'a>>;

/// Transport implemented by provider adapters after they pass the conformance suite.
///
/// There is deliberately no string/XML fallback: successful transports emit
/// Codex `ResponseEvent` values or fail closed.
pub trait CanonicalTransport: std::fmt::Debug + Send + Sync {
    type Error: Error + Send + Sync + 'static;

    fn provider_id(&self) -> &str;

    fn compatibility_grade(&self) -> CompatibilityGrade;

    fn capabilities(&self) -> ProviderCapabilities;

    fn stream<'a>(&'a self, request: &'a ResponsesApiRequest) -> TransportFuture<'a, Self::Error>;
}

#[cfg(test)]
mod tests {
    use super::ProviderCapabilities;
    use super::WireProtocol;

    #[test]
    fn native_responses_capabilities_preserve_full_protocol() {
        let capabilities = ProviderCapabilities::native_responses();
        assert_eq!(capabilities.wire_protocol, WireProtocol::OpenAiResponses);
        assert!(capabilities.streaming);
        assert!(capabilities.native_tool_calls);
        assert!(capabilities.parallel_tool_calls);
        assert!(capabilities.response_continuity);
    }
}
