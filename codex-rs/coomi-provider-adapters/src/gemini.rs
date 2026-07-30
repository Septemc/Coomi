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
use crate::common::text_content;
use crate::common::wire_tool_name;
use codex_protocol::ResponseItemId;
use codex_protocol::models::ContentItem;
use codex_protocol::protocol::TokenUsage;
use serde_json::Map;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeSet;
use std::collections::HashMap;

#[derive(Clone, Copy, Debug, Default)]
pub struct GeminiNativeAdapter;

#[derive(Debug, Default)]
pub struct GeminiDecodeState {
    response_id: Option<String>,
    message_started: bool,
    message_done: bool,
    text: String,
    seen_tool_slots: BTreeSet<(u64, u64)>,
    saw_tool_call: bool,
    usage: Option<TokenUsage>,
    end_turn: Option<bool>,
    completed: bool,
}

impl WireAdapter for GeminiNativeAdapter {
    type DecodeState = GeminiDecodeState;

    fn protocol(&self) -> WireProtocol {
        WireProtocol::GeminiNative
    }

    fn encode_request(
        &self,
        request: &ResponsesApiRequest,
    ) -> Result<EncodedRequest, AdapterError> {
        let protocol = self.protocol();
        let (system, contents) = encode_contents(protocol, request)?;
        let declarations = canonical_tools(request, protocol)?
            .iter()
            .map(|tool| {
                let parts = function_tool_parts(protocol, tool)?;
                let mut declaration = Map::from_iter([
                    ("name".to_string(), Value::String(parts.name.to_string())),
                    ("parameters".to_string(), parts.parameters),
                ]);
                if let Some(description) = parts.description {
                    declaration.insert(
                        "description".to_string(),
                        Value::String(description.to_string()),
                    );
                }
                Ok(Value::Object(declaration))
            })
            .collect::<Result<Vec<_>, AdapterError>>()?;

        let mut body = Map::from_iter([("contents".to_string(), Value::Array(contents))]);
        if !system.is_empty() {
            body.insert(
                "systemInstruction".to_string(),
                json!({"parts": [{"text": system}]}),
            );
        }
        if !declarations.is_empty() {
            body.insert(
                "tools".to_string(),
                json!([{"functionDeclarations": declarations}]),
            );
            body.insert(
                "toolConfig".to_string(),
                json!({"functionCallingConfig": {"mode": "AUTO"}}),
            );
        }

        Ok(EncodedRequest {
            path: format!("models/{}:streamGenerateContent?alt=sse", request.model),
            body: Value::Object(body),
        })
    }

    fn decode_event(
        &self,
        state: &mut Self::DecodeState,
        event: WireEvent,
    ) -> Result<Vec<ResponseEvent>, AdapterError> {
        let WireEvent::Json { data, .. } = event else {
            return finish_stream(state);
        };
        if let Some(error) = data.get("error") {
            return Err(AdapterError::Provider(error.to_string()));
        }
        if let Some(response_id) = data
            .get("responseId")
            .or_else(|| data.get("response_id"))
            .and_then(Value::as_str)
        {
            state.response_id = Some(response_id.to_string());
        }
        if let Some(usage) = data.get("usageMetadata") {
            state.usage = Some(gemini_usage(usage));
        }

        let mut events = Vec::new();
        for (candidate_index, candidate) in data
            .get("candidates")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .enumerate()
        {
            let parts = candidate
                .get("content")
                .and_then(|content| content.get("parts"))
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            for (part_index, part) in parts.into_iter().enumerate() {
                if part.get("thought").and_then(Value::as_bool) == Some(true) {
                    continue;
                }
                if let Some(text) = part.get("text").and_then(Value::as_str)
                    && !text.is_empty()
                {
                    start_message(state, &mut events);
                    state.text.push_str(text);
                    events.push(ResponseEvent::OutputTextDelta(text.to_string()));
                }
                if let Some(call) = part.get("functionCall") {
                    decode_function_call(
                        state,
                        candidate_index as u64,
                        part_index as u64,
                        call,
                        &mut events,
                    )?;
                }
                if part.get("functionResponse").is_some() {
                    return Err(AdapterError::UnsupportedEvent {
                        protocol: self.protocol(),
                        event_type: "functionResponse in model output".to_string(),
                    });
                }
            }
            if let Some(reason) = candidate.get("finishReason").and_then(Value::as_str) {
                state.end_turn = Some(reason == "STOP" && !state.saw_tool_call);
                finish_message(state, &mut events);
            }
        }
        Ok(events)
    }
}

fn encode_contents(
    protocol: WireProtocol,
    request: &ResponsesApiRequest,
) -> Result<(String, Vec<Value>), AdapterError> {
    let mut system = request.instructions.clone();
    let mut contents = Vec::new();
    let call_names = request
        .input
        .iter()
        .filter_map(|item| match item {
            ResponseItem::FunctionCall {
                call_id,
                name,
                namespace,
                ..
            } => Some((call_id.clone(), wire_tool_name(namespace.as_deref(), name))),
            ResponseItem::ToolSearchCall {
                call_id: Some(call_id),
                ..
            } => Some((call_id.clone(), "tool_search".to_string())),
            ResponseItem::CustomToolCall {
                call_id,
                name,
                namespace,
                ..
            } => Some((call_id.clone(), wire_tool_name(namespace.as_deref(), name))),
            _ => None,
        })
        .collect::<HashMap<_, _>>();

    for item in &request.input {
        match item {
            ResponseItem::Message { role, content, .. }
                if role == "developer" || role == "system" =>
            {
                let text = text_content(content);
                if !text.is_empty() {
                    if !system.is_empty() {
                        system.push_str("\n\n");
                    }
                    system.push_str(&text);
                }
            }
            ResponseItem::Message { role, content, .. } => {
                let role = if role == "assistant" { "model" } else { "user" };
                let parts = content
                    .iter()
                    .map(|item| match item {
                        ContentItem::InputText { text } | ContentItem::OutputText { text } => {
                            Ok(json!({"text": text}))
                        }
                        ContentItem::InputImage { image_url, .. } => {
                            Ok(json!({"fileData": {"fileUri": image_url}}))
                        }
                        ContentItem::InputAudio { .. } => Err(AdapterError::UnsupportedInput {
                            protocol,
                            item_type: "input_audio",
                        }),
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                push_parts(&mut contents, role, parts);
            }
            ResponseItem::FunctionCall {
                name,
                namespace,
                arguments,
                ..
            } => {
                let args: Value = serde_json::from_str(arguments).map_err(|error| {
                    AdapterError::InvalidPayload {
                        protocol,
                        message: format!("tool `{name}` arguments are not JSON: {error}"),
                    }
                })?;
                push_parts(
                    &mut contents,
                    "model",
                    vec![json!({
                        "functionCall": {
                            "name": wire_tool_name(namespace.as_deref(), name),
                            "args": args,
                        }
                    })],
                );
            }
            ResponseItem::ToolSearchCall { arguments, .. } => push_parts(
                &mut contents,
                "model",
                vec![json!({
                    "functionCall": {"name": "tool_search", "args": arguments}
                })],
            ),
            ResponseItem::CustomToolCall {
                name,
                namespace,
                input,
                ..
            } => push_parts(
                &mut contents,
                "model",
                vec![json!({
                    "functionCall": {
                        "name": wire_tool_name(namespace.as_deref(), name),
                        "args": {"input": input},
                    }
                })],
            ),
            ResponseItem::FunctionCallOutput {
                call_id, output, ..
            }
            | ResponseItem::CustomToolCallOutput {
                call_id, output, ..
            } => {
                let name = call_names
                    .get(call_id)
                    .ok_or_else(|| AdapterError::InvalidPayload {
                        protocol,
                        message: format!("tool output `{call_id}` has no matching call"),
                    })?;
                let output =
                    output
                        .body
                        .to_text()
                        .ok_or_else(|| AdapterError::UnsupportedInput {
                            protocol,
                            item_type: "structured_function_call_output",
                        })?;
                push_parts(
                    &mut contents,
                    "user",
                    vec![json!({
                        "functionResponse": {
                            "name": name,
                            "response": {"output": output},
                        }
                    })],
                );
            }
            ResponseItem::ToolSearchOutput {
                call_id: Some(call_id),
                tools,
                ..
            } => {
                let name = call_names
                    .get(call_id)
                    .ok_or_else(|| AdapterError::InvalidPayload {
                        protocol,
                        message: format!("tool output `{call_id}` has no matching call"),
                    })?;
                push_parts(
                    &mut contents,
                    "user",
                    vec![json!({
                        "functionResponse": {
                            "name": name,
                            "response": {"tools": tools},
                        }
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
    Ok((system, contents))
}

fn push_parts(contents: &mut Vec<Value>, role: &str, parts: Vec<Value>) {
    if let Some(last) = contents.last_mut()
        && last.get("role").and_then(Value::as_str) == Some(role)
        && let Some(existing) = last.get_mut("parts").and_then(Value::as_array_mut)
    {
        existing.extend(parts);
        return;
    }
    contents.push(json!({"role": role, "parts": parts}));
}

fn start_message(state: &mut GeminiDecodeState, events: &mut Vec<ResponseEvent>) {
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

fn decode_function_call(
    state: &mut GeminiDecodeState,
    candidate_index: u64,
    part_index: u64,
    call: &Value,
    events: &mut Vec<ResponseEvent>,
) -> Result<(), AdapterError> {
    let slot = (candidate_index, part_index);
    if !state.seen_tool_slots.insert(slot) {
        return Err(AdapterError::InvalidPayload {
            protocol: WireProtocol::GeminiNative,
            message: format!("function call slot {candidate_index}:{part_index} repeated"),
        });
    }
    let name = call
        .get("name")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("functionCall is missing `name`"))?
        .to_string();
    let call_id = call
        .get("id")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| format!("call_gemini_{candidate_index}_{part_index}"));
    let args = call
        .get("args")
        .cloned()
        .unwrap_or_else(|| Value::Object(Map::new()));
    let arguments = serde_json::to_string(&args)?;
    let item_id = format!("fc_{call_id}");
    state.saw_tool_call = true;
    events.push(ResponseEvent::OutputItemAdded(ResponseItem::FunctionCall {
        id: Some(ResponseItemId::from_server(item_id.clone())),
        name: name.clone(),
        namespace: None,
        arguments: String::new(),
        encrypted_function_args: None,
        call_id: call_id.clone(),
        internal_chat_message_metadata_passthrough: None,
    }));
    events.push(ResponseEvent::ToolCallInputDelta {
        item_id,
        call_id: Some(call_id.clone()),
        delta: arguments.clone(),
    });
    events.push(ResponseEvent::OutputItemDone(ResponseItem::FunctionCall {
        id: Some(ResponseItemId::from_server(format!("fc_{call_id}"))),
        name,
        namespace: None,
        arguments,
        encrypted_function_args: None,
        call_id,
        internal_chat_message_metadata_passthrough: None,
    }));
    Ok(())
}

fn finish_message(state: &mut GeminiDecodeState, events: &mut Vec<ResponseEvent>) {
    if !state.message_started || state.message_done {
        return;
    }
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

fn finish_stream(state: &mut GeminiDecodeState) -> Result<Vec<ResponseEvent>, AdapterError> {
    if state.completed {
        return Ok(Vec::new());
    }
    let mut events = Vec::new();
    finish_message(state, &mut events);
    state.completed = true;
    events.push(ResponseEvent::Completed {
        response_id: state
            .response_id
            .clone()
            .unwrap_or_else(|| "coomi-gemini-response".to_string()),
        token_usage: state.usage.clone(),
        end_turn: state.end_turn,
    });
    Ok(events)
}

fn gemini_usage(usage: &Value) -> TokenUsage {
    let input_tokens = integer(usage.get("promptTokenCount"));
    let cached_input_tokens = integer(usage.get("cachedContentTokenCount"));
    let output_tokens = integer(usage.get("candidatesTokenCount"));
    let reasoning_output_tokens = integer(usage.get("thoughtsTokenCount"));
    let total_tokens = integer(usage.get("totalTokenCount"))
        .max(input_tokens + output_tokens + reasoning_output_tokens);
    TokenUsage {
        input_tokens,
        cached_input_tokens,
        cache_write_input_tokens: 0,
        output_tokens,
        reasoning_output_tokens,
        total_tokens,
    }
}

fn message_id(state: &GeminiDecodeState) -> String {
    format!(
        "msg_{}",
        state.response_id.as_deref().unwrap_or("coomi_gemini")
    )
}

fn invalid(message: impl Into<String>) -> AdapterError {
    AdapterError::InvalidPayload {
        protocol: WireProtocol::GeminiNative,
        message: message.into(),
    }
}
