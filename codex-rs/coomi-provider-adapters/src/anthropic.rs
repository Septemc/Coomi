use crate::AdapterError;
use crate::EncodedRequest;
use crate::ResponseEvent;
use crate::ResponseItem;
use crate::ResponsesApiRequest;
use crate::WireAdapter;
use crate::WireEvent;
use crate::WireProtocol;
use crate::common::canonical_tools;
use crate::common::function_tool_parts;
use crate::common::integer;
use crate::common::response_item_kind;
use codex_protocol::ResponseItemId;
use codex_protocol::models::ContentItem;
use codex_protocol::protocol::TokenUsage;
use serde_json::Map;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;

const DEFAULT_MAX_OUTPUT_TOKENS: i64 = 16_384;

#[derive(Clone, Copy, Debug, Default)]
pub struct AnthropicMessagesAdapter;

#[derive(Debug, Default)]
pub struct AnthropicDecodeState {
    response_id: Option<String>,
    message_started: bool,
    message_done: bool,
    text: String,
    blocks: BTreeMap<u64, AnthropicBlock>,
    usage: TokenUsage,
    saw_usage: bool,
    end_turn: Option<bool>,
    completed: bool,
}

#[derive(Debug)]
enum AnthropicBlock {
    Text,
    Tool {
        call_id: String,
        name: String,
        arguments: String,
        done: bool,
    },
    Ignored,
}

impl WireAdapter for AnthropicMessagesAdapter {
    type DecodeState = AnthropicDecodeState;

    fn protocol(&self) -> WireProtocol {
        WireProtocol::AnthropicMessages
    }

    fn encode_request(
        &self,
        request: &ResponsesApiRequest,
    ) -> Result<EncodedRequest, AdapterError> {
        let protocol = self.protocol();
        let (system, messages) = encode_messages(protocol, request)?;
        let tools = canonical_tools(request)?
            .iter()
            .map(|tool| {
                let parts = function_tool_parts(protocol, tool)?;
                let mut encoded = Map::from_iter([
                    ("name".to_string(), Value::String(parts.name.to_string())),
                    ("input_schema".to_string(), parts.parameters),
                ]);
                if let Some(description) = parts.description {
                    encoded.insert(
                        "description".to_string(),
                        Value::String(description.to_string()),
                    );
                }
                Ok(Value::Object(encoded))
            })
            .collect::<Result<Vec<_>, AdapterError>>()?;

        let mut body = Map::from_iter([
            ("model".to_string(), Value::String(request.model.clone())),
            ("messages".to_string(), Value::Array(messages)),
            ("stream".to_string(), Value::Bool(true)),
            (
                "max_tokens".to_string(),
                Value::Number(DEFAULT_MAX_OUTPUT_TOKENS.into()),
            ),
        ]);
        if !system.is_empty() {
            body.insert("system".to_string(), Value::String(system));
        }
        if !tools.is_empty() {
            body.insert("tools".to_string(), Value::Array(tools));
            body.insert("tool_choice".to_string(), json!({"type": "auto"}));
        }

        Ok(EncodedRequest {
            path: "messages".to_string(),
            body: Value::Object(body),
        })
    }

    fn decode_event(
        &self,
        state: &mut Self::DecodeState,
        event: WireEvent,
    ) -> Result<Vec<ResponseEvent>, AdapterError> {
        let WireEvent::Json { event_type, data } = event else {
            return finish_stream(state);
        };
        if let Some(error) = data.get("error") {
            return Err(AdapterError::Provider(error.to_string()));
        }
        let event_type = event_type
            .or_else(|| data.get("type").and_then(Value::as_str).map(str::to_owned))
            .unwrap_or_default();
        let mut events = Vec::new();
        match event_type.as_str() {
            "message_start" => {
                let message = data.get("message").unwrap_or(&data);
                state.response_id = message.get("id").and_then(Value::as_str).map(str::to_owned);
                merge_usage(state, message.get("usage"));
                events.push(ResponseEvent::Created);
            }
            "content_block_start" => {
                let index = required_index(&data)?;
                let block = data
                    .get("content_block")
                    .ok_or_else(|| invalid("content_block_start is missing `content_block`"))?;
                let block_type = block
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                match block_type {
                    "text" => {
                        start_message(state, &mut events);
                        state.blocks.insert(index, AnthropicBlock::Text);
                        if let Some(text) = block.get("text").and_then(Value::as_str)
                            && !text.is_empty()
                        {
                            state.text.push_str(text);
                            events.push(ResponseEvent::OutputTextDelta(text.to_string()));
                        }
                    }
                    "tool_use" => {
                        let call_id = required_string(block, "id")?.to_string();
                        let name = required_string(block, "name")?.to_string();
                        let item_id = format!("fc_{call_id}");
                        events.push(ResponseEvent::OutputItemAdded(ResponseItem::FunctionCall {
                            id: Some(ResponseItemId::from_server(item_id.clone())),
                            name: name.clone(),
                            namespace: None,
                            arguments: String::new(),
                            encrypted_function_args: None,
                            call_id: call_id.clone(),
                            internal_chat_message_metadata_passthrough: None,
                        }));
                        let initial = block
                            .get("input")
                            .filter(|input| !input.as_object().is_some_and(Map::is_empty))
                            .map(serde_json::to_string)
                            .transpose()?
                            .unwrap_or_default();
                        if !initial.is_empty() {
                            events.push(ResponseEvent::ToolCallInputDelta {
                                item_id,
                                call_id: Some(call_id.clone()),
                                delta: initial.clone(),
                            });
                        }
                        state.blocks.insert(
                            index,
                            AnthropicBlock::Tool {
                                call_id,
                                name,
                                arguments: initial,
                                done: false,
                            },
                        );
                    }
                    "thinking" | "redacted_thinking" => {
                        state.blocks.insert(index, AnthropicBlock::Ignored);
                    }
                    unknown if unknown.contains("tool") => {
                        return Err(AdapterError::UnsupportedEvent {
                            protocol: self.protocol(),
                            event_type: unknown.to_string(),
                        });
                    }
                    _ => {
                        state.blocks.insert(index, AnthropicBlock::Ignored);
                    }
                }
            }
            "content_block_delta" => {
                let index = required_index(&data)?;
                let delta = data
                    .get("delta")
                    .ok_or_else(|| invalid("content_block_delta is missing `delta`"))?;
                let delta_type = delta
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                match (state.blocks.get_mut(&index), delta_type) {
                    (Some(AnthropicBlock::Text), "text_delta") => {
                        let text = required_string(delta, "text")?.to_string();
                        state.text.push_str(&text);
                        events.push(ResponseEvent::OutputTextDelta(text));
                    }
                    (
                        Some(AnthropicBlock::Tool {
                            call_id, arguments, ..
                        }),
                        "input_json_delta",
                    ) => {
                        let partial = required_string(delta, "partial_json")?.to_string();
                        arguments.push_str(&partial);
                        events.push(ResponseEvent::ToolCallInputDelta {
                            item_id: format!("fc_{call_id}"),
                            call_id: Some(call_id.clone()),
                            delta: partial,
                        });
                    }
                    (Some(AnthropicBlock::Ignored), "thinking_delta" | "signature_delta") => {}
                    (_, unknown) if unknown.contains("tool") || unknown.contains("json") => {
                        return Err(AdapterError::UnsupportedEvent {
                            protocol: self.protocol(),
                            event_type: unknown.to_string(),
                        });
                    }
                    _ => {}
                }
            }
            "content_block_stop" => {
                let index = required_index(&data)?;
                finish_block(state, index, &mut events)?;
            }
            "message_delta" => {
                merge_usage(state, data.get("usage"));
                if let Some(reason) = data
                    .get("delta")
                    .and_then(|delta| delta.get("stop_reason"))
                    .and_then(Value::as_str)
                {
                    state.end_turn = Some(reason != "tool_use");
                }
            }
            "message_stop" => return finish_stream_with_events(state, events),
            "ping" => {}
            unknown if unknown.contains("tool") => {
                return Err(AdapterError::UnsupportedEvent {
                    protocol: self.protocol(),
                    event_type: unknown.to_string(),
                });
            }
            _ => {}
        }
        Ok(events)
    }
}

fn encode_messages(
    protocol: WireProtocol,
    request: &ResponsesApiRequest,
) -> Result<(String, Vec<Value>), AdapterError> {
    let mut system = request.instructions.clone();
    let mut messages = Vec::new();
    for item in &request.input {
        match item {
            ResponseItem::Message { role, content, .. }
                if role == "developer" || role == "system" =>
            {
                let text = content
                    .iter()
                    .filter_map(|item| match item {
                        ContentItem::InputText { text } | ContentItem::OutputText { text } => {
                            Some(text.as_str())
                        }
                        _ => None,
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                if !text.is_empty() {
                    if !system.is_empty() {
                        system.push_str("\n\n");
                    }
                    system.push_str(&text);
                }
            }
            ResponseItem::Message { role, content, .. } => {
                let role = if role == "assistant" {
                    "assistant"
                } else {
                    "user"
                };
                let blocks = content
                    .iter()
                    .map(|item| match item {
                        ContentItem::InputText { text } | ContentItem::OutputText { text } => {
                            Ok(json!({"type": "text", "text": text}))
                        }
                        ContentItem::InputImage { image_url, .. } => Ok(json!({
                            "type": "image",
                            "source": {"type": "url", "url": image_url},
                        })),
                        ContentItem::InputAudio { .. } => Err(AdapterError::UnsupportedInput {
                            protocol,
                            item_type: "input_audio",
                        }),
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                push_blocks(&mut messages, role, blocks);
            }
            ResponseItem::FunctionCall {
                name,
                arguments,
                call_id,
                ..
            } => {
                let input: Value = serde_json::from_str(arguments).map_err(|error| {
                    AdapterError::InvalidPayload {
                        protocol,
                        message: format!("tool `{name}` arguments are not JSON: {error}"),
                    }
                })?;
                push_blocks(
                    &mut messages,
                    "assistant",
                    vec![json!({
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": input,
                    })],
                );
            }
            ResponseItem::FunctionCallOutput {
                call_id, output, ..
            } => {
                let content =
                    output
                        .body
                        .to_text()
                        .ok_or_else(|| AdapterError::UnsupportedInput {
                            protocol,
                            item_type: "structured_function_call_output",
                        })?;
                push_blocks(
                    &mut messages,
                    "user",
                    vec![json!({
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": content,
                    })],
                );
            }
            ResponseItem::Reasoning { .. } => {}
            unsupported => {
                return Err(AdapterError::UnsupportedInput {
                    protocol,
                    item_type: response_item_kind(unsupported),
                });
            }
        }
    }
    Ok((system, messages))
}

fn push_blocks(messages: &mut Vec<Value>, role: &str, blocks: Vec<Value>) {
    if let Some(last) = messages.last_mut()
        && last.get("role").and_then(Value::as_str) == Some(role)
        && let Some(content) = last.get_mut("content").and_then(Value::as_array_mut)
    {
        content.extend(blocks);
        return;
    }
    messages.push(json!({"role": role, "content": blocks}));
}

fn start_message(state: &mut AnthropicDecodeState, events: &mut Vec<ResponseEvent>) {
    if state.message_started {
        return;
    }
    state.message_started = true;
    events.push(ResponseEvent::OutputItemAdded(ResponseItem::Message {
        id: Some(ResponseItemId::from_server(message_id(state))),
        role: "assistant".to_string(),
        content: Vec::new(),
        phase: None,
        internal_chat_message_metadata_passthrough: None,
    }));
}

fn finish_block(
    state: &mut AnthropicDecodeState,
    index: u64,
    events: &mut Vec<ResponseEvent>,
) -> Result<(), AdapterError> {
    let Some(block) = state.blocks.get_mut(&index) else {
        return Err(invalid(format!(
            "content block {index} stopped before start"
        )));
    };
    if let AnthropicBlock::Tool {
        call_id,
        name,
        arguments,
        done,
    } = block
        && !*done
    {
        *done = true;
        events.push(ResponseEvent::OutputItemDone(ResponseItem::FunctionCall {
            id: Some(ResponseItemId::from_server(format!("fc_{call_id}"))),
            name: name.clone(),
            namespace: None,
            arguments: arguments.clone(),
            encrypted_function_args: None,
            call_id: call_id.clone(),
            internal_chat_message_metadata_passthrough: None,
        }));
    }
    Ok(())
}

fn finish_stream(state: &mut AnthropicDecodeState) -> Result<Vec<ResponseEvent>, AdapterError> {
    finish_stream_with_events(state, Vec::new())
}

fn finish_stream_with_events(
    state: &mut AnthropicDecodeState,
    mut events: Vec<ResponseEvent>,
) -> Result<Vec<ResponseEvent>, AdapterError> {
    if state.completed {
        return Ok(events);
    }
    let indexes = state.blocks.keys().copied().collect::<Vec<_>>();
    for index in indexes {
        finish_block(state, index, &mut events)?;
    }
    if state.message_started && !state.message_done {
        state.message_done = true;
        events.push(ResponseEvent::OutputItemDone(ResponseItem::Message {
            id: Some(ResponseItemId::from_server(message_id(state))),
            role: "assistant".to_string(),
            content: vec![ContentItem::OutputText {
                text: state.text.clone(),
            }],
            phase: None,
            internal_chat_message_metadata_passthrough: None,
        }));
    }
    state.completed = true;
    events.push(ResponseEvent::Completed {
        response_id: state
            .response_id
            .clone()
            .unwrap_or_else(|| "coomi-anthropic-response".to_string()),
        token_usage: state.saw_usage.then_some(state.usage.clone()),
        end_turn: state.end_turn,
    });
    Ok(events)
}

fn merge_usage(state: &mut AnthropicDecodeState, usage: Option<&Value>) {
    let Some(usage) = usage else {
        return;
    };
    state.saw_usage = true;
    let uncached = integer(usage.get("input_tokens"));
    let cached = integer(usage.get("cache_read_input_tokens"));
    let cache_write = integer(usage.get("cache_creation_input_tokens"));
    if uncached != 0 || cached != 0 || cache_write != 0 {
        state.usage.input_tokens = uncached + cached + cache_write;
        state.usage.cached_input_tokens = cached;
        state.usage.cache_write_input_tokens = cache_write;
    }
    let output = integer(usage.get("output_tokens"));
    if output != 0 {
        state.usage.output_tokens = output;
    }
    state.usage.total_tokens = state.usage.input_tokens + state.usage.output_tokens;
}

fn message_id(state: &AnthropicDecodeState) -> String {
    format!(
        "msg_{}",
        state.response_id.as_deref().unwrap_or("coomi_anthropic")
    )
}

fn required_index(data: &Value) -> Result<u64, AdapterError> {
    data.get("index")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid("event is missing numeric `index`"))
}

fn required_string<'a>(data: &'a Value, field: &str) -> Result<&'a str, AdapterError> {
    data.get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid(format!("payload is missing string `{field}`")))
}

fn invalid(message: impl Into<String>) -> AdapterError {
    AdapterError::InvalidPayload {
        protocol: WireProtocol::AnthropicMessages,
        message: message.into(),
    }
}
