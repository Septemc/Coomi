use crate::AdapterError;
use crate::ResponsesApiRequest;
use crate::WireProtocol;
use codex_protocol::models::ContentItem;
use codex_protocol::models::ResponseItem;
use codex_protocol::protocol::TokenUsage;
use serde_json::Value;

pub(crate) fn request_json(request: &ResponsesApiRequest) -> Result<Value, AdapterError> {
    serde_json::to_value(request).map_err(AdapterError::from)
}

pub(crate) fn canonical_tools(request: &ResponsesApiRequest) -> Result<Vec<Value>, AdapterError> {
    let request = request_json(request)?;
    Ok(request
        .get("tools")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default())
}

pub(crate) struct FunctionToolParts<'a> {
    pub name: &'a str,
    pub description: Option<&'a str>,
    pub parameters: Value,
    pub strict: Option<bool>,
}

pub(crate) fn function_tool_parts(
    protocol: WireProtocol,
    tool: &Value,
) -> Result<FunctionToolParts<'_>, AdapterError> {
    let tool_type = tool
        .get("type")
        .and_then(Value::as_str)
        .unwrap_or("unknown");
    if tool_type != "function" {
        return Err(AdapterError::UnsupportedTool {
            protocol,
            tool_type: tool_type.to_string(),
        });
    }
    let name =
        tool.get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| AdapterError::InvalidPayload {
                protocol,
                message: "function tool is missing `name`".to_string(),
            })?;
    let description = tool.get("description").and_then(Value::as_str);
    let parameters = tool
        .get("parameters")
        .cloned()
        .unwrap_or_else(|| serde_json::json!({"type": "object"}));
    let strict = tool.get("strict").and_then(Value::as_bool);
    Ok(FunctionToolParts {
        name,
        description,
        parameters,
        strict,
    })
}

pub(crate) fn response_item_kind(item: &ResponseItem) -> &'static str {
    match item {
        ResponseItem::AdditionalTools { .. } => "additional_tools",
        ResponseItem::Message { .. } => "message",
        ResponseItem::AgentMessage { .. } => "agent_message",
        ResponseItem::Reasoning { .. } => "reasoning",
        ResponseItem::LocalShellCall { .. } => "local_shell_call",
        ResponseItem::FunctionCall { .. } => "function_call",
        ResponseItem::ToolSearchCall { .. } => "tool_search_call",
        ResponseItem::FunctionCallOutput { .. } => "function_call_output",
        ResponseItem::CustomToolCall { .. } => "custom_tool_call",
        ResponseItem::CustomToolCallOutput { .. } => "custom_tool_call_output",
        ResponseItem::ToolSearchOutput { .. } => "tool_search_output",
        ResponseItem::WebSearchCall { .. } => "web_search_call",
        ResponseItem::ImageGenerationCall { .. } => "image_generation_call",
        ResponseItem::Compaction { .. } => "compaction",
        ResponseItem::CompactionTrigger { .. } => "compaction_trigger",
        ResponseItem::ContextCompaction { .. } => "context_compaction",
        ResponseItem::Other => "other",
    }
}

pub(crate) fn text_content(content: &[ContentItem]) -> String {
    content
        .iter()
        .filter_map(|item| match item {
            ContentItem::InputText { text } | ContentItem::OutputText { text } => {
                Some(text.as_str())
            }
            ContentItem::InputImage { .. } | ContentItem::InputAudio { .. } => None,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

pub(crate) fn openai_usage(value: Option<&Value>) -> Option<TokenUsage> {
    let usage = value?.as_object()?;
    let input_tokens = integer(
        usage
            .get("input_tokens")
            .or_else(|| usage.get("prompt_tokens")),
    );
    let output_tokens = integer(
        usage
            .get("output_tokens")
            .or_else(|| usage.get("completion_tokens")),
    );
    let cached_input_tokens = usage
        .get("input_tokens_details")
        .or_else(|| usage.get("prompt_tokens_details"))
        .and_then(|details| details.get("cached_tokens"))
        .map_or(0, |value| integer(Some(value)));
    let reasoning_output_tokens = usage
        .get("output_tokens_details")
        .or_else(|| usage.get("completion_tokens_details"))
        .and_then(|details| details.get("reasoning_tokens"))
        .map_or(0, |value| integer(Some(value)));
    let total_tokens = integer(usage.get("total_tokens")).max(input_tokens + output_tokens);
    Some(TokenUsage {
        input_tokens,
        cached_input_tokens,
        cache_write_input_tokens: 0,
        output_tokens,
        reasoning_output_tokens,
        total_tokens,
    })
}

pub(crate) fn integer(value: Option<&Value>) -> i64 {
    value.and_then(Value::as_i64).unwrap_or_default()
}
