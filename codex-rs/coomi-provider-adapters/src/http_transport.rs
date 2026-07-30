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
use crate::ResponseItem;
use crate::ResponsesApiRequest;
use crate::ResponsesDecodeState;
use crate::TransportFuture;
use crate::WireAdapter;
use crate::WireEvent;
use crate::WireProtocol;
use crate::common::CanonicalWireTool;
use crate::common::canonical_tool_name_map;
use async_stream::try_stream;
use codex_tools::validate_json_schema_value;
use eventsource_stream::Eventsource;
use futures::StreamExt;
use reqwest::header::ACCEPT;
use reqwest::header::AUTHORIZATION;
use reqwest::header::CONTENT_TYPE;
use reqwest::header::HeaderMap;
use reqwest::header::HeaderValue;
use serde_json::Value;
use std::collections::HashMap;
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

    pub async fn stream_owned(
        &self,
        request: ResponsesApiRequest,
    ) -> Result<CanonicalEventStream<'static, AdapterError>, AdapterError> {
        let protocol = self.provider.protocol;
        let tool_names = (protocol != WireProtocol::OpenAiResponses)
            .then(|| canonical_tool_name_map(&request, protocol))
            .transpose()?;
        let encoded = self.adapter.encode_request(&request)?;
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
                for mut canonical in adapter.decode_event(&mut state, wire_event)? {
                    if let Some(tool_names) = tool_names.as_ref() {
                        restore_canonical_tool_name(&mut canonical, tool_names, protocol)?;
                    }
                    yield canonical;
                }
            }
            for mut canonical in adapter.decode_event(&mut state, WireEvent::Done)? {
                if let Some(tool_names) = tool_names.as_ref() {
                    restore_canonical_tool_name(&mut canonical, tool_names, protocol)?;
                }
                yield canonical;
            }
        };
        Ok(Box::pin(stream))
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
            let stream = self.stream_owned(request.clone()).await?;
            let stream: CanonicalEventStream<'a, AdapterError> = stream;
            Ok(stream)
        })
    }
}

fn restore_canonical_tool_name(
    event: &mut ResponseEvent,
    tool_names: &HashMap<String, CanonicalWireTool>,
    protocol: WireProtocol,
) -> Result<(), AdapterError> {
    let is_done = matches!(event, ResponseEvent::OutputItemDone(_));
    let item = match event {
        ResponseEvent::OutputItemAdded(item) | ResponseEvent::OutputItemDone(item) => item,
        _ => return Ok(()),
    };
    let ResponseItem::FunctionCall { name, .. } = item else {
        return Ok(());
    };
    let canonical = tool_names
        .get(name)
        .cloned()
        .ok_or_else(|| AdapterError::InvalidPayload {
            protocol,
            message: format!("provider called undeclared tool `{name}`"),
        })?;
    let replacement = match canonical {
        CanonicalWireTool::Function { namespace, name } => {
            let ResponseItem::FunctionCall {
                id,
                arguments,
                encrypted_function_args,
                call_id,
                internal_chat_message_metadata_passthrough,
                ..
            } = item.clone()
            else {
                unreachable!("matched a function call above");
            };
            ResponseItem::FunctionCall {
                id,
                name,
                namespace,
                arguments,
                encrypted_function_args,
                call_id,
                internal_chat_message_metadata_passthrough,
            }
        }
        CanonicalWireTool::ToolSearch { parameters } => {
            let ResponseItem::FunctionCall {
                id,
                arguments,
                call_id,
                internal_chat_message_metadata_passthrough,
                ..
            } = item.clone()
            else {
                unreachable!("matched a function call above");
            };
            let arguments = if is_done {
                let value = serde_json::from_str(&arguments).map_err(|error| {
                    AdapterError::InvalidPayload {
                        protocol,
                        message: format!("tool_search returned malformed JSON arguments: {error}"),
                    }
                })?;
                validate_json_schema_value(parameters.as_ref(), &value).map_err(|error| {
                    AdapterError::InvalidPayload {
                        protocol,
                        message: format!("tool_search returned invalid arguments: {error}"),
                    }
                })?;
                value
            } else {
                Value::Null
            };
            ResponseItem::ToolSearchCall {
                id,
                call_id: Some(call_id),
                status: Some("completed".to_string()),
                execution: "client".to_string(),
                arguments,
                internal_chat_message_metadata_passthrough,
            }
        }
        CanonicalWireTool::Freeform { name } => {
            let ResponseItem::FunctionCall {
                id,
                arguments,
                call_id,
                internal_chat_message_metadata_passthrough,
                ..
            } = item.clone()
            else {
                unreachable!("matched a function call above");
            };
            let input = if is_done {
                let value: Value = serde_json::from_str(&arguments).map_err(|error| {
                    AdapterError::InvalidPayload {
                        protocol,
                        message: format!("freeform tool `{name}` returned malformed JSON: {error}"),
                    }
                })?;
                let object = value
                    .as_object()
                    .ok_or_else(|| AdapterError::InvalidPayload {
                        protocol,
                        message: format!("freeform tool `{name}` arguments must be an object"),
                    })?;
                if object.len() != 1 {
                    return Err(AdapterError::InvalidPayload {
                        protocol,
                        message: format!(
                            "freeform tool `{name}` arguments must contain only `input`"
                        ),
                    });
                }
                object
                    .get("input")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
                    .ok_or_else(|| AdapterError::InvalidPayload {
                        protocol,
                        message: format!(
                            "freeform tool `{name}` arguments require a string `input`"
                        ),
                    })?
            } else {
                String::new()
            };
            ResponseItem::CustomToolCall {
                id,
                status: Some("completed".to_string()),
                call_id,
                name,
                namespace: None,
                input,
                internal_chat_message_metadata_passthrough,
            }
        }
    };
    *item = replacement;
    Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;
    use codex_tools::AdditionalProperties;
    use codex_tools::JsonSchema;
    use std::collections::BTreeMap;

    fn function_call(name: &str, arguments: &str) -> ResponseItem {
        ResponseItem::FunctionCall {
            id: None,
            name: name.to_string(),
            namespace: None,
            arguments: arguments.to_string(),
            encrypted_function_args: None,
            call_id: "call-1".to_string(),
            internal_chat_message_metadata_passthrough: None,
        }
    }

    #[test]
    fn restores_reduced_special_tool_events_to_canonical_items() {
        let tool_search_schema = JsonSchema::object(
            BTreeMap::from([(
                "query".to_string(),
                JsonSchema::string(Some("Search query".to_string())),
            )]),
            Some(vec!["query".to_string()]),
            Some(AdditionalProperties::Boolean(false)),
        );
        let tool_names = HashMap::from([
            (
                "tool_search".to_string(),
                CanonicalWireTool::ToolSearch {
                    parameters: Box::new(tool_search_schema),
                },
            ),
            (
                "apply_patch".to_string(),
                CanonicalWireTool::Freeform {
                    name: "apply_patch".to_string(),
                },
            ),
        ]);

        let mut search = ResponseEvent::OutputItemDone(function_call(
            "tool_search",
            r#"{"query":"filesystem"}"#,
        ));
        restore_canonical_tool_name(&mut search, &tool_names, WireProtocol::OpenAiCompatible)
            .expect("restore tool_search");
        assert!(matches!(
            search,
            ResponseEvent::OutputItemDone(ResponseItem::ToolSearchCall {
                call_id: Some(ref call_id),
                ref arguments,
                ref execution,
                ..
            }) if call_id == "call-1"
                && arguments == &serde_json::json!({"query": "filesystem"})
                && execution == "client"
        ));

        let mut freeform = ResponseEvent::OutputItemDone(function_call(
            "apply_patch",
            r#"{"input":"*** Begin Patch"}"#,
        ));
        restore_canonical_tool_name(&mut freeform, &tool_names, WireProtocol::OpenAiCompatible)
            .expect("restore freeform tool");
        assert!(matches!(
            freeform,
            ResponseEvent::OutputItemDone(ResponseItem::CustomToolCall {
                ref call_id,
                ref name,
                ref input,
                ..
            }) if call_id == "call-1"
                && name == "apply_patch"
                && input == "*** Begin Patch"
        ));
    }

    #[test]
    fn rejects_undeclared_and_malformed_reduced_tool_calls() {
        let tool_names = HashMap::from([
            (
                "tool_search".to_string(),
                CanonicalWireTool::ToolSearch {
                    parameters: Box::new(JsonSchema::object(
                        BTreeMap::from([("query".to_string(), JsonSchema::string(None))]),
                        Some(vec!["query".to_string()]),
                        Some(AdditionalProperties::Boolean(false)),
                    )),
                },
            ),
            (
                "apply_patch".to_string(),
                CanonicalWireTool::Freeform {
                    name: "apply_patch".to_string(),
                },
            ),
        ]);

        let mut undeclared = ResponseEvent::OutputItemAdded(function_call("invented_tool", ""));
        let error = restore_canonical_tool_name(
            &mut undeclared,
            &tool_names,
            WireProtocol::OpenAiCompatible,
        )
        .expect_err("undeclared tools must fail closed");
        assert!(error.to_string().contains("undeclared tool"));

        let mut invalid_search = ResponseEvent::OutputItemDone(function_call(
            "tool_search",
            r#"{"provider_field":true}"#,
        ));
        let error = restore_canonical_tool_name(
            &mut invalid_search,
            &tool_names,
            WireProtocol::OpenAiCompatible,
        )
        .expect_err("invalid tool_search arguments must fail closed");
        assert!(error.to_string().contains("invalid arguments"));

        let mut invalid_freeform =
            ResponseEvent::OutputItemDone(function_call("apply_patch", r#"{"input":7}"#));
        let error = restore_canonical_tool_name(
            &mut invalid_freeform,
            &tool_names,
            WireProtocol::OpenAiCompatible,
        )
        .expect_err("invalid freeform wrapper must fail closed");
        assert!(error.to_string().contains("string `input`"));
    }

    #[test]
    fn restores_flattened_namespace_function_names() {
        let tool_names = HashMap::from([(
            "filesystem__read".to_string(),
            CanonicalWireTool::Function {
                namespace: Some("filesystem".to_string()),
                name: "read".to_string(),
            },
        )]);
        let mut event = ResponseEvent::OutputItemDone(function_call(
            "filesystem__read",
            r#"{"path":"README.md"}"#,
        ));

        restore_canonical_tool_name(&mut event, &tool_names, WireProtocol::OpenAiCompatible)
            .expect("restore namespaced function");

        let ResponseEvent::OutputItemDone(ResponseItem::FunctionCall {
            namespace, name, ..
        }) = event
        else {
            panic!("expected function call");
        };
        assert_eq!(namespace.as_deref(), Some("filesystem"));
        assert_eq!(name, "read");
    }
}
