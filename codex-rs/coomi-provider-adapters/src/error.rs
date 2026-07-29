use crate::WireProtocol;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum AdapterError {
    #[error("failed to serialize canonical request: {0}")]
    Serialize(#[from] serde_json::Error),

    #[error("{protocol:?} does not support canonical input item `{item_type}`")]
    UnsupportedInput {
        protocol: WireProtocol,
        item_type: &'static str,
    },

    #[error("{protocol:?} does not support canonical tool `{tool_type}`")]
    UnsupportedTool {
        protocol: WireProtocol,
        tool_type: String,
    },

    #[error("{protocol:?} emitted unsupported event `{event_type}`")]
    UnsupportedEvent {
        protocol: WireProtocol,
        event_type: String,
    },

    #[error("invalid {protocol:?} payload: {message}")]
    InvalidPayload {
        protocol: WireProtocol,
        message: String,
    },

    #[error("provider returned an error: {0}")]
    Provider(String),

    #[error("failed to read provider config `{path}`: {source}")]
    ConfigIo {
        path: String,
        #[source]
        source: std::io::Error,
    },

    #[error("invalid provider configuration: {0}")]
    Config(String),

    #[error("provider HTTP request failed: {0}")]
    Http(#[from] reqwest::Error),

    #[error("provider returned HTTP {status}: {body}")]
    HttpStatus { status: u16, body: String },

    #[error("provider event stream failed: {0}")]
    EventStream(String),
}
