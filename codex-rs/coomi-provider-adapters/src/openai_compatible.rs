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
use crate::common::openai_usage;
use crate::common::response_item_kind;
use crate::common::wire_tool_name;
use codex_protocol::ResponseItemId;
use codex_protocol::models::ContentItem;
use codex_protocol::models::ReasoningItemContent;
use codex_protocol::protocol::TokenUsage;
use serde::Deserialize;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;

#[derive(Clone, Copy, Debug, Default)]
pub struct OpenAiCompatibleAdapter;

#[derive(Debug, Default)]
pub struct OpenAiCompatibleDecodeState {
    response_id: Option<String>,
    reasoning_id: Option<String>,
    reasoning_started: bool,
    reasoning_done: bool,
    reasoning_text: String,
    message_id: Option<String>,
    message_started: bool,
    message_done: bool,
    text: String,
    tool_calls: BTreeMap<u64, PendingToolCall>,
    usage: Option<TokenUsage>,
    end_turn: Option<bool>,
    completed: bool,
}

#[derive(Debug, Default)]
struct PendingToolCall {
    call_id: Option<String>,
    name: String,
    arguments: String,
    item_started: bool,
    item_done: bool,
}

#[derive(Debug, Deserialize)]
struct ChatChunk {
    id: Option<String>,
    #[serde(default)]
    choices: Vec<ChatChoice>,
    usage: Option<Value>,
}

#[derive(Debug, Deserialize)]
struct ChatChoice {
    delta: ChatDelta,
    finish_reason: Option<String>,
}

#[derive(Debug, Default, Deserialize)]
struct ChatDelta {
    content: Option<String>,
    reasoning_content: Option<String>,
    #[serde(default)]
    tool_calls: Vec<ChatToolCallDelta>,
}

#[derive(Debug, Deserialize)]
struct ChatToolCallDelta {
    index: u64,
    id: Option<String>,
    function: Option<ChatFunctionDelta>,
}

#[derive(Debug, Deserialize)]
struct ChatFunctionDelta {
    name: Option<String>,
    arguments: Option<String>,
}

impl WireAdapter for OpenAiCompatibleAdapter {
    type DecodeState = OpenAiCompatibleDecodeState;

    fn protocol(&self) -> WireProtocol {
        WireProtocol::OpenAiCompatible
    }

    fn encode_request(
        &self,
        request: &ResponsesApiRequest,
    ) -> Result<EncodedRequest, AdapterError> {
        let protocol = self.protocol();
        let messages = encode_messages(protocol, request)?;
        let tools = canonical_tools(request, protocol)?
            .iter()
            .map(|tool| {
                let parts = function_tool_parts(protocol, tool)?;
                let mut function = serde_json::Map::from_iter([
                    ("name".to_string(), Value::String(parts.name.to_string())),
                    ("parameters".to_string(), parts.parameters),
                ]);
                if let Some(description) = parts.description {
                    function.insert(
                        "description".to_string(),
                        Value::String(description.to_string()),
                    );
                }
                // Chat-compatible providers are not trusted to enforce strict
                // JSON Schema. Core validates arguments before dispatch.
                function.insert("strict".to_string(), Value::Bool(false));
                Ok(json!({"type": "function", "function": function}))
            })
            .collect::<Result<Vec<_>, AdapterError>>()?;

        let mut body = serde_json::Map::from_iter([
            ("model".to_string(), Value::String(request.model.clone())),
            ("messages".to_string(), Value::Array(messages)),
            ("stream".to_string(), Value::Bool(true)),
            ("stream_options".to_string(), json!({"include_usage": true})),
            (
                "parallel_tool_calls".to_string(),
                Value::Bool(request.parallel_tool_calls),
            ),
        ]);
        if !tools.is_empty() {
            body.insert("tools".to_string(), Value::Array(tools));
            body.insert(
                "tool_choice".to_string(),
                Value::String(request.tool_choice.clone()),
            );
        }

        Ok(EncodedRequest {
            path: "chat/completions".to_string(),
            body: Value::Object(body),
        })
    }

    fn decode_event(
        &self,
        state: &mut Self::DecodeState,
        event: WireEvent,
    ) -> Result<Vec<ResponseEvent>, AdapterError> {
        match event {
            WireEvent::Done => finish_stream(state),
            WireEvent::Json { data, .. } => {
                if let Some(error) = data.get("error") {
                    return Err(AdapterError::Provider(error.to_string()));
                }
                let chunk: ChatChunk = serde_json::from_value(data)?;
                if let Some(response_id) = chunk.id {
                    state.response_id = Some(response_id);
                }
                if let Some(usage) = openai_usage(chunk.usage.as_ref()) {
                    state.usage = Some(usage);
                }

                let mut events = Vec::new();
                for choice in chunk.choices {
                    if let Some(reasoning) = choice.delta.reasoning_content
                        && !reasoning.is_empty()
                    {
                        start_reasoning(state, &mut events)?;
                        state.reasoning_text.push_str(&reasoning);
                        events.push(ResponseEvent::ReasoningContentDelta {
                            delta: reasoning,
                            content_index: 0,
                        });
                    }
                    if let Some(content) = choice.delta.content
                        && !content.is_empty()
                    {
                        finish_reasoning(state, &mut events);
                        if !state.tool_calls.is_empty() {
                            return Err(invalid_transition(
                                "assistant content arrived after a tool call started",
                            ));
                        }
                        start_message(state, &mut events);
                        state.text.push_str(&content);
                        events.push(ResponseEvent::OutputTextDelta(content));
                    }
                    for tool_delta in choice.delta.tool_calls {
                        finish_reasoning(state, &mut events);
                        finish_message(state, &mut events);
                        decode_tool_delta(state, tool_delta, &mut events)?;
                    }
                    if let Some(reason) = choice.finish_reason {
                        state.end_turn = Some(reason != "tool_calls" && reason != "function_call");
                        finish_items(state, &mut events)?;
                    }
                }
                Ok(events)
            }
        }
    }
}

fn encode_messages(
    protocol: WireProtocol,
    request: &ResponsesApiRequest,
) -> Result<Vec<Value>, AdapterError> {
    let mut messages = Vec::new();
    if !request.instructions.is_empty() {
        messages.push(json!({"role": "system", "content": request.instructions}));
    }

    for item in &request.input {
        match item {
            ResponseItem::Message { role, content, .. } => {
                let role = match role.as_str() {
                    "developer" | "system" => "system",
                    other => other,
                };
                messages.push(json!({
                    "role": role,
                    "content": encode_chat_content(protocol, content)?,
                }));
            }
            ResponseItem::FunctionCall {
                name,
                namespace,
                arguments,
                call_id,
                ..
            } => messages.push(json!({
                "role": "assistant",
                "content": Value::Null,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": wire_tool_name(namespace.as_deref(), name),
                        "arguments": arguments,
                    },
                }],
            })),
            ResponseItem::ToolSearchCall {
                call_id: Some(call_id),
                arguments,
                ..
            } => messages.push(chat_tool_call(
                call_id,
                "tool_search",
                serde_json::to_string(arguments)?,
            )),
            ResponseItem::CustomToolCall {
                name,
                namespace,
                input,
                call_id,
                ..
            } => messages.push(chat_tool_call(
                call_id,
                &wire_tool_name(namespace.as_deref(), name),
                serde_json::to_string(&json!({"input": input}))?,
            )),
            ResponseItem::FunctionCallOutput {
                call_id, output, ..
            }
            | ResponseItem::CustomToolCallOutput {
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
                messages.push(json!({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                }));
            }
            ResponseItem::ToolSearchOutput {
                call_id: Some(call_id),
                tools,
                ..
            } => messages.push(json!({
                "role": "tool",
                "tool_call_id": call_id,
                "content": serde_json::to_string(tools)?,
            })),
            ResponseItem::Reasoning { .. } => {}
            unsupported => {
                return Err(AdapterError::UnsupportedInput {
                    protocol,
                    item_type: response_item_kind(unsupported),
                });
            }
        }
    }
    Ok(messages)
}

fn chat_tool_call(call_id: &str, name: &str, arguments: String) -> Value {
    json!({
        "role": "assistant",
        "content": Value::Null,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }],
    })
}

fn encode_chat_content(
    protocol: WireProtocol,
    content: &[ContentItem],
) -> Result<Value, AdapterError> {
    if content.len() == 1
        && let ContentItem::InputText { text } | ContentItem::OutputText { text } = &content[0]
    {
        return Ok(Value::String(text.clone()));
    }

    let parts = content
        .iter()
        .map(|item| match item {
            ContentItem::InputText { text } | ContentItem::OutputText { text } => {
                Ok(json!({"type": "text", "text": text}))
            }
            ContentItem::InputImage { image_url, .. } => {
                Ok(json!({"type": "image_url", "image_url": {"url": image_url}}))
            }
            ContentItem::InputAudio { .. } => Err(AdapterError::UnsupportedInput {
                protocol,
                item_type: "input_audio",
            }),
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(Value::Array(parts))
}

fn start_message(state: &mut OpenAiCompatibleDecodeState, events: &mut Vec<ResponseEvent>) {
    if state.message_started {
        return;
    }
    let message_id = state
        .message_id
        .get_or_insert_with(|| "msg_coomi_chat".to_string())
        .clone();
    state.message_started = true;
    events.push(ResponseEvent::OutputItemAdded(ResponseItem::Message {
        id: Some(ResponseItemId::from_server(message_id)),
        role: "assistant".to_string(),
        content: Vec::new(),
        phase: None,
        internal_chat_message_metadata_passthrough: None,
    }));
}

fn start_reasoning(
    state: &mut OpenAiCompatibleDecodeState,
    events: &mut Vec<ResponseEvent>,
) -> Result<(), AdapterError> {
    if state.reasoning_started {
        if state.reasoning_done {
            return Err(invalid_transition(
                "reasoning content resumed after the reasoning item completed",
            ));
        }
        return Ok(());
    }
    if state.message_started || !state.tool_calls.is_empty() {
        return Err(invalid_transition(
            "reasoning content arrived after assistant output started",
        ));
    }

    let reasoning_id = state
        .reasoning_id
        .get_or_insert_with(|| {
            state
                .response_id
                .as_deref()
                .map_or_else(|| "rs_coomi_chat".to_string(), |id| format!("rs_{id}"))
        })
        .clone();
    state.reasoning_started = true;
    events.push(ResponseEvent::OutputItemAdded(ResponseItem::Reasoning {
        id: Some(ResponseItemId::from_server(reasoning_id)),
        summary: Vec::new(),
        content: Some(Vec::new()),
        encrypted_content: None,
        internal_chat_message_metadata_passthrough: None,
    }));
    Ok(())
}

fn finish_reasoning(state: &mut OpenAiCompatibleDecodeState, events: &mut Vec<ResponseEvent>) {
    if !state.reasoning_started || state.reasoning_done {
        return;
    }
    state.reasoning_done = true;
    events.push(ResponseEvent::OutputItemDone(ResponseItem::Reasoning {
        id: state.reasoning_id.clone().map(ResponseItemId::from_server),
        summary: Vec::new(),
        content: Some(vec![ReasoningItemContent::ReasoningText {
            text: state.reasoning_text.clone(),
        }]),
        encrypted_content: None,
        internal_chat_message_metadata_passthrough: None,
    }));
}

fn finish_message(state: &mut OpenAiCompatibleDecodeState, events: &mut Vec<ResponseEvent>) {
    if !state.message_started || state.message_done {
        return;
    }
    state.message_done = true;
    events.push(ResponseEvent::OutputItemDone(ResponseItem::Message {
        id: state.message_id.clone().map(ResponseItemId::from_server),
        role: "assistant".to_string(),
        content: vec![ContentItem::OutputText {
            text: state.text.clone(),
        }],
        phase: None,
        internal_chat_message_metadata_passthrough: None,
    }));
}

fn invalid_transition(message: impl Into<String>) -> AdapterError {
    AdapterError::InvalidPayload {
        protocol: WireProtocol::OpenAiCompatible,
        message: message.into(),
    }
}

fn decode_tool_delta(
    state: &mut OpenAiCompatibleDecodeState,
    delta: ChatToolCallDelta,
    events: &mut Vec<ResponseEvent>,
) -> Result<(), AdapterError> {
    let pending = state.tool_calls.entry(delta.index).or_default();
    if let Some(call_id) = delta.id {
        if pending
            .call_id
            .as_ref()
            .is_some_and(|current| current != &call_id)
        {
            return Err(AdapterError::InvalidPayload {
                protocol: WireProtocol::OpenAiCompatible,
                message: format!("tool index {} changed call id", delta.index),
            });
        }
        pending.call_id = Some(call_id);
    }
    let function = delta.function.unwrap_or(ChatFunctionDelta {
        name: None,
        arguments: None,
    });
    if let Some(name) = function.name {
        pending.name.push_str(&name);
    }
    let Some(call_id) = pending.call_id.clone() else {
        if function
            .arguments
            .as_deref()
            .is_some_and(|value| !value.is_empty())
        {
            return Err(AdapterError::InvalidPayload {
                protocol: WireProtocol::OpenAiCompatible,
                message: format!(
                    "tool index {} emitted arguments before call id",
                    delta.index
                ),
            });
        }
        return Ok(());
    };
    let item_id = format!("fc_{call_id}");
    if !pending.item_started {
        if pending.name.is_empty() {
            return Ok(());
        }
        pending.item_started = true;
        events.push(ResponseEvent::OutputItemAdded(ResponseItem::FunctionCall {
            id: Some(ResponseItemId::from_server(item_id.clone())),
            name: pending.name.clone(),
            namespace: None,
            arguments: String::new(),
            encrypted_function_args: None,
            call_id: call_id.clone(),
            internal_chat_message_metadata_passthrough: None,
        }));
    }
    if let Some(arguments) = function.arguments
        && !arguments.is_empty()
    {
        pending.arguments.push_str(&arguments);
        events.push(ResponseEvent::ToolCallInputDelta {
            item_id,
            call_id: Some(call_id),
            delta: arguments,
        });
    }
    Ok(())
}

fn finish_items(
    state: &mut OpenAiCompatibleDecodeState,
    events: &mut Vec<ResponseEvent>,
) -> Result<(), AdapterError> {
    finish_reasoning(state, events);
    finish_message(state, events);
    for (index, tool) in &mut state.tool_calls {
        if tool.item_done {
            continue;
        }
        let call_id = tool
            .call_id
            .clone()
            .ok_or_else(|| AdapterError::InvalidPayload {
                protocol: WireProtocol::OpenAiCompatible,
                message: format!("tool index {index} completed without call id"),
            })?;
        if tool.name.is_empty() {
            return Err(AdapterError::InvalidPayload {
                protocol: WireProtocol::OpenAiCompatible,
                message: format!("tool index {index} completed without name"),
            });
        }
        tool.item_done = true;
        events.push(ResponseEvent::OutputItemDone(ResponseItem::FunctionCall {
            id: Some(ResponseItemId::from_server(format!("fc_{call_id}"))),
            name: tool.name.clone(),
            namespace: None,
            arguments: tool.arguments.clone(),
            encrypted_function_args: None,
            call_id,
            internal_chat_message_metadata_passthrough: None,
        }));
    }
    Ok(())
}

fn finish_stream(
    state: &mut OpenAiCompatibleDecodeState,
) -> Result<Vec<ResponseEvent>, AdapterError> {
    if state.completed {
        return Ok(Vec::new());
    }
    let mut events = Vec::new();
    finish_items(state, &mut events)?;
    state.completed = true;
    events.push(ResponseEvent::Completed {
        response_id: state
            .response_id
            .clone()
            .unwrap_or_else(|| "coomi-chat-response".to_string()),
        token_usage: state.usage.clone(),
        end_turn: state.end_turn,
    });
    Ok(events)
}
