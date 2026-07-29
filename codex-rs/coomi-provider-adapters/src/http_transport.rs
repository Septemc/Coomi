use crate::AdapterError;
use crate::AnthropicDecodeState;
use crate::AnthropicMessagesAdapter;
use crate::CanonicalEventStream;
use crate::CanonicalTransport;
use crate::CompatibilityGrade;
use crate::GeminiDecodeState;
use crate::GeminiNativeAdapter;
use crate::OpenAiCompatibleAdapter;
use crate::OpenAiCompatibleDecodeState;
use crate::OpenAiResponsesAdapter;
use crate::ProviderCapabilities;
use crate::ProviderConfig;
use crate::ResponseEvent;
use crate::ResponsesApiRequest;
use crate::ResponsesDecodeState;
use crate::TransportFuture;
use crate::WireAdapter;
use crate::WireEvent;
use crate::WireProtocol;
use async_stream::try_stream;
use eventsource_stream::Eventsource;
use futures::StreamExt;
use reqwest::header::ACCEPT;
use reqwest::header::AUTHORIZATION;
use reqwest::header::CONTENT_TYPE;
use reqwest::header::HeaderMap;
use reqwest::header::HeaderValue;
use std::sync::Arc;
use url::Url;

#[derive(Clone, Debug)]
pub enum ProviderAdapter {
    OpenAiResponses(OpenAiResponsesAdapter),
    OpenAiCompatible(OpenAiCompatibleAdapter),
    AnthropicMessages(AnthropicMessagesAdapter),
    GeminiNative(GeminiNativeAdapter),
}

enum ProviderDecodeState {
    OpenAiResponses(ResponsesDecodeState),
    OpenAiCompatible(OpenAiCompatibleDecodeState),
    AnthropicMessages(AnthropicDecodeState),
    GeminiNative(GeminiDecodeState),
}

impl ProviderAdapter {
    pub fn for_protocol(protocol: WireProtocol) -> Self {
        match protocol {
            WireProtocol::OpenAiResponses => Self::OpenAiResponses(OpenAiResponsesAdapter),
            WireProtocol::OpenAiCompatible => Self::OpenAiCompatible(OpenAiCompatibleAdapter),
            WireProtocol::AnthropicMessages => Self::AnthropicMessages(AnthropicMessagesAdapter),
            WireProtocol::GeminiNative => Self::GeminiNative(GeminiNativeAdapter),
        }
    }

    fn new_state(&self) -> ProviderDecodeState {
        match self {
            Self::OpenAiResponses(_) => {
                ProviderDecodeState::OpenAiResponses(ResponsesDecodeState::default())
            }
            Self::OpenAiCompatible(_) => {
                ProviderDecodeState::OpenAiCompatible(OpenAiCompatibleDecodeState::default())
            }
            Self::AnthropicMessages(_) => {
                ProviderDecodeState::AnthropicMessages(AnthropicDecodeState::default())
            }
            Self::GeminiNative(_) => {
                ProviderDecodeState::GeminiNative(GeminiDecodeState::default())
            }
        }
    }

    fn encode_request(
        &self,
        request: &ResponsesApiRequest,
    ) -> Result<crate::EncodedRequest, AdapterError> {
        match self {
            Self::OpenAiResponses(adapter) => adapter.encode_request(request),
            Self::OpenAiCompatible(adapter) => adapter.encode_request(request),
            Self::AnthropicMessages(adapter) => adapter.encode_request(request),
            Self::GeminiNative(adapter) => adapter.encode_request(request),
        }
    }

    fn decode_event(
        &self,
        state: &mut ProviderDecodeState,
        event: WireEvent,
    ) -> Result<Vec<ResponseEvent>, AdapterError> {
        match (self, state) {
            (Self::OpenAiResponses(adapter), ProviderDecodeState::OpenAiResponses(state)) => {
                adapter.decode_event(state, event)
            }
            (Self::OpenAiCompatible(adapter), ProviderDecodeState::OpenAiCompatible(state)) => {
                adapter.decode_event(state, event)
            }
            (Self::AnthropicMessages(adapter), ProviderDecodeState::AnthropicMessages(state)) => {
                adapter.decode_event(state, event)
            }
            (Self::GeminiNative(adapter), ProviderDecodeState::GeminiNative(state)) => {
                adapter.decode_event(state, event)
            }
            _ => Err(AdapterError::Config(
                "provider adapter and decode state do not match".to_string(),
            )),
        }
    }
}

#[derive(Clone, Debug)]
pub struct HttpProviderTransport {
    provider: Arc<ProviderConfig>,
    adapter: ProviderAdapter,
    client: reqwest::Client,
}

impl HttpProviderTransport {
    pub fn new(provider: ProviderConfig) -> Result<Self, AdapterError> {
        let adapter = ProviderAdapter::for_protocol(provider.protocol);
        let client = reqwest::Client::builder().build()?;
        Ok(Self {
            provider: Arc::new(provider),
            adapter,
            client,
        })
    }

    fn headers(&self) -> Result<HeaderMap, AdapterError> {
        let mut headers = HeaderMap::new();
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        headers.insert(ACCEPT, HeaderValue::from_static("text/event-stream"));
        let key = self.provider.api_key.expose_secret();
        match self.provider.protocol {
            WireProtocol::OpenAiResponses | WireProtocol::OpenAiCompatible => {
                let value = HeaderValue::from_str(&format!("Bearer {key}"))
                    .map_err(|error| AdapterError::Config(error.to_string()))?;
                headers.insert(AUTHORIZATION, value);
            }
            WireProtocol::AnthropicMessages => {
                headers.insert(
                    "x-api-key",
                    HeaderValue::from_str(key)
                        .map_err(|error| AdapterError::Config(error.to_string()))?,
                );
                headers.insert("anthropic-version", HeaderValue::from_static("2023-06-01"));
            }
            WireProtocol::GeminiNative => {
                headers.insert(
                    "x-goog-api-key",
                    HeaderValue::from_str(key)
                        .map_err(|error| AdapterError::Config(error.to_string()))?,
                );
            }
        }
        Ok(headers)
    }
}

impl CanonicalTransport for HttpProviderTransport {
    type Error = AdapterError;

    fn provider_id(&self) -> &str {
        &self.provider.id
    }

    fn compatibility_grade(&self) -> CompatibilityGrade {
        match self.provider.protocol {
            WireProtocol::OpenAiResponses => CompatibilityGrade::Native,
            WireProtocol::OpenAiCompatible => CompatibilityGrade::Compatible,
            WireProtocol::AnthropicMessages | WireProtocol::GeminiNative => {
                CompatibilityGrade::Experimental
            }
        }
    }

    fn capabilities(&self) -> ProviderCapabilities {
        match self.provider.protocol {
            WireProtocol::OpenAiResponses => ProviderCapabilities::native_responses(),
            WireProtocol::OpenAiCompatible => ProviderCapabilities {
                wire_protocol: self.provider.protocol,
                streaming: true,
                native_tool_calls: true,
                parallel_tool_calls: true,
                strict_json_schema: false,
                reasoning_summary: false,
                response_continuity: false,
            },
            WireProtocol::AnthropicMessages => ProviderCapabilities {
                wire_protocol: self.provider.protocol,
                streaming: true,
                native_tool_calls: true,
                parallel_tool_calls: true,
                strict_json_schema: true,
                reasoning_summary: false,
                response_continuity: false,
            },
            WireProtocol::GeminiNative => ProviderCapabilities {
                wire_protocol: self.provider.protocol,
                streaming: true,
                native_tool_calls: true,
                parallel_tool_calls: true,
                strict_json_schema: false,
                reasoning_summary: false,
                response_continuity: false,
            },
        }
    }

    fn stream<'a>(&'a self, request: &'a ResponsesApiRequest) -> TransportFuture<'a, Self::Error> {
        Box::pin(async move {
            let encoded = self.adapter.encode_request(request)?;
            let url = join_endpoint(&self.provider.base_url, &encoded.path)?;
            let response = self
                .client
                .post(url)
                .headers(self.headers()?)
                .json(&encoded.body)
                .send()
                .await?;
            let status = response.status();
            if !status.is_success() {
                let body = response.text().await.unwrap_or_default();
                return Err(AdapterError::HttpStatus {
                    status: status.as_u16(),
                    body: body.chars().take(4_096).collect(),
                });
            }

            let adapter = self.adapter.clone();
            let mut state = adapter.new_state();
            let stream = try_stream! {
                let mut upstream = response.bytes_stream().eventsource();
                while let Some(event) = upstream.next().await {
                    let event = event.map_err(|error| AdapterError::EventStream(error.to_string()))?;
                    let wire_event = if event.data.trim() == "[DONE]" {
                        WireEvent::Done
                    } else {
                        WireEvent::Json {
                            event_type: (!event.event.is_empty()).then_some(event.event),
                            data: serde_json::from_str(&event.data)?,
                        }
                    };
                    for canonical in adapter.decode_event(&mut state, wire_event)? {
                        yield canonical;
                    }
                }
                for canonical in adapter.decode_event(&mut state, WireEvent::Done)? {
                    yield canonical;
                }
            };
            let stream: CanonicalEventStream<'a, AdapterError> = Box::pin(stream);
            Ok(stream)
        })
    }
}

fn join_endpoint(base_url: &Url, path: &str) -> Result<Url, AdapterError> {
    let mut base = base_url.as_str().trim_end_matches('/').to_string();
    base.push('/');
    base.push_str(path.trim_start_matches('/'));
    Url::parse(&base).map_err(|error| {
        AdapterError::Config(format!(
            "failed to join provider base URL and `{path}`: {error}"
        ))
    })
}
