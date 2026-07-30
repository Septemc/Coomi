use codex_protocol::models::ContentItem;
use codex_protocol::models::FunctionCallOutputPayload;
use codex_protocol::models::ReasoningItemContent;
use codex_tools::AdditionalProperties;
use codex_tools::FreeformTool;
use codex_tools::FreeformToolFormat;
use codex_tools::JsonSchema;
use codex_tools::ResponsesApiTool;
use codex_tools::create_tools_raw_json_for_responses_api;
use coomi_provider_adapters::AdapterError;
use coomi_provider_adapters::AnthropicDecodeState;
use coomi_provider_adapters::AnthropicMessagesAdapter;
use coomi_provider_adapters::GeminiDecodeState;
use coomi_provider_adapters::GeminiNativeAdapter;
use coomi_provider_adapters::OpenAiCompatibleAdapter;
use coomi_provider_adapters::OpenAiCompatibleDecodeState;
use coomi_provider_adapters::OpenAiResponsesAdapter;
use coomi_provider_adapters::ResponseEvent;
use coomi_provider_adapters::ResponseItem;
use coomi_provider_adapters::ResponsesApiRequest;
use coomi_provider_adapters::ResponsesDecodeState;
use coomi_provider_adapters::ToolSpec;
use coomi_provider_adapters::WireAdapter;
use coomi_provider_adapters::WireEvent;
use serde_json::Value;
use serde_json::json;
use std::collections::BTreeMap;

fn canonical_request(input: Vec<ResponseItem>) -> Result<ResponsesApiRequest, serde_json::Error> {
    let tool = ToolSpec::Function(ResponsesApiTool {
        name: "read_file".to_string(),
        description: "Read one workspace file".to_string(),
        strict: true,
        defer_loading: None,
        parameters: JsonSchema::object(
            BTreeMap::from([(
                "path".to_string(),
                JsonSchema::string(Some("Workspace-relative path".to_string())),
            )]),
            Some(vec!["path".to_string()]),
            Some(AdditionalProperties::Boolean(false)),
        ),
        output_schema: None,
    });
    Ok(ResponsesApiRequest {
        model: "deepseek-v4-flash".to_string(),
        instructions: "You are Coomi.".to_string(),
        input,
        tools: Some(create_tools_raw_json_for_responses_api(&[tool])?.into()),
        tool_choice: "auto".to_string(),
        parallel_tool_calls: true,
        reasoning: None,
        store: false,
        stream: true,
        stream_options: None,
        include: Vec::new(),
        service_tier: None,
        prompt_cache_key: Some("coomi-test".to_string()),
        text: None,
        client_metadata: None,
    })
}

fn request_with_tools(
    input: Vec<ResponseItem>,
    tools: Vec<ToolSpec>,
) -> Result<ResponsesApiRequest, serde_json::Error> {
    let mut request = canonical_request(input)?;
    request.tools = Some(create_tools_raw_json_for_responses_api(&tools)?.into());
    Ok(request)
}

fn user_message(text: &str) -> ResponseItem {
    ResponseItem::Message {
        id: None,
        role: "user".to_string(),
        content: vec![ContentItem::InputText {
            text: text.to_string(),
        }],
        phase: None,
        internal_chat_message_metadata_passthrough: None,
    }
}

fn tool_round_trip_input() -> Vec<ResponseItem> {
    vec![
        user_message("Read src/lib.rs"),
        ResponseItem::FunctionCall {
            id: None,
            name: "read_file".to_string(),
            namespace: None,
            arguments: r#"{"path":"src/lib.rs"}"#.to_string(),
            encrypted_function_args: None,
            call_id: "call_123".to_string(),
            internal_chat_message_metadata_passthrough: None,
        },
        ResponseItem::FunctionCallOutput {
            id: None,
            call_id: "call_123".to_string(),
            output: FunctionCallOutputPayload::from_text("file body".to_string()),
            internal_chat_message_metadata_passthrough: None,
        },
    ]
}

fn json_event(data: Value) -> WireEvent {
    WireEvent::Json {
        event_type: None,
        data,
    }
}

#[test]
fn native_responses_preserves_the_canonical_request() {
    let adapter = OpenAiResponsesAdapter;
    let request =
        canonical_request(vec![user_message("hello")]).expect("build canonical Responses request");
    let encoded = adapter.encode_request(&request).expect("encode Responses");
    assert_eq!(encoded.path, "responses");
    assert_eq!(encoded.body["model"], "deepseek-v4-flash");
    assert_eq!(encoded.body["tools"][0]["name"], "read_file");

    let mut state = ResponsesDecodeState::default();
    let events = adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}
                }
            })),
        )
        .expect("decode completed event");
    assert!(matches!(
        events.as_slice(),
        [ResponseEvent::Completed { response_id, .. }] if response_id == "resp_1"
    ));
}

#[test]
fn openai_compatible_maps_tools_results_and_streamed_arguments() {
    let adapter = OpenAiCompatibleAdapter;
    let request = canonical_request(tool_round_trip_input()).expect("build canonical chat request");
    let encoded = adapter
        .encode_request(&request)
        .expect("encode chat request");
    assert_eq!(encoded.path, "chat/completions");
    assert_eq!(encoded.body["tools"][0]["function"]["name"], "read_file");
    assert!(encoded.body["messages"].as_array().is_some_and(|messages| {
        messages
            .iter()
            .any(|message| message["role"] == "tool" && message["tool_call_id"] == "call_123")
    }));

    let mut state = OpenAiCompatibleDecodeState::default();
    let first = adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "id": "chatcmpl_1",
                "choices": [{
                    "delta": {"tool_calls": [{
                        "index": 0,
                        "id": "call_live",
                        "function": {"name": "read_file", "arguments": "{\"path\":\"src/"}
                    }]},
                    "finish_reason": null
                }]
            })),
        )
        .expect("decode first chat chunk");
    assert!(first.iter().any(|event| matches!(
        event,
        ResponseEvent::ToolCallInputDelta { call_id: Some(call_id), delta, .. }
            if call_id == "call_live" && delta == "{\"path\":\"src/"
    )));
    let second = adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "id": "chatcmpl_1",
                "choices": [{
                    "delta": {"tool_calls": [{
                        "index": 0,
                        "function": {"arguments": "lib.rs\"}"}
                    }]},
                    "finish_reason": "tool_calls"
                }],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "total_tokens": 13,
                    "prompt_tokens_details": {"cached_tokens": 4}
                }
            })),
        )
        .expect("decode final chat chunk");
    assert!(second.iter().any(|event| matches!(
        event,
        ResponseEvent::OutputItemDone(ResponseItem::FunctionCall {
            call_id,
            arguments,
            ..
        }) if call_id == "call_live" && arguments == r#"{"path":"src/lib.rs"}"#
    )));
    let done = adapter
        .decode_event(&mut state, WireEvent::Done)
        .expect("finish chat stream");
    assert!(matches!(
        done.as_slice(),
        [ResponseEvent::Completed {
            token_usage: Some(usage),
            end_turn: Some(false),
            ..
        }] if usage.cached_input_tokens == 4 && usage.total_tokens == 13
    ));
}

#[test]
fn openai_compatible_emits_a_complete_reasoning_item_lifecycle() {
    let adapter = OpenAiCompatibleAdapter;
    let mut state = OpenAiCompatibleDecodeState::default();

    let first = adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "id": "chatcmpl_reasoning",
                "choices": [{
                    "delta": {"reasoning_content": "inspect "},
                    "finish_reason": null
                }]
            })),
        )
        .expect("decode first reasoning chunk");
    assert!(matches!(
        first.as_slice(),
        [
            ResponseEvent::OutputItemAdded(ResponseItem::Reasoning { .. }),
            ResponseEvent::ReasoningContentDelta { delta, content_index: 0 }
        ] if delta == "inspect "
    ));

    let second = adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "id": "chatcmpl_reasoning",
                "choices": [{
                    "delta": {"reasoning_content": "then answer"},
                    "finish_reason": null
                }]
            })),
        )
        .expect("decode second reasoning chunk");
    assert!(matches!(
        second.as_slice(),
        [ResponseEvent::ReasoningContentDelta { delta, content_index: 0 }]
            if delta == "then answer"
    ));

    let answer = adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "id": "chatcmpl_reasoning",
                "choices": [{
                    "delta": {"content": "done"},
                    "finish_reason": "stop"
                }]
            })),
        )
        .expect("decode answer chunk");
    assert!(matches!(
        answer.as_slice(),
        [
            ResponseEvent::OutputItemDone(ResponseItem::Reasoning {
                content: Some(reasoning),
                ..
            }),
            ResponseEvent::OutputItemAdded(ResponseItem::Message { .. }),
            ResponseEvent::OutputTextDelta(delta),
            ResponseEvent::OutputItemDone(ResponseItem::Message { .. }),
        ] if reasoning == &[ReasoningItemContent::ReasoningText {
            text: "inspect then answer".to_string(),
        }] && delta == "done"
    ));
}

#[test]
fn openai_compatible_rejects_reasoning_after_message_output() {
    let adapter = OpenAiCompatibleAdapter;
    let mut state = OpenAiCompatibleDecodeState::default();
    adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "id": "chatcmpl_late_reasoning",
                "choices": [{"delta": {"content": "answer"}, "finish_reason": null}]
            })),
        )
        .expect("decode answer chunk");

    let error = adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "id": "chatcmpl_late_reasoning",
                "choices": [{
                    "delta": {"reasoning_content": "too late"},
                    "finish_reason": null
                }]
            })),
        )
        .expect_err("late reasoning must fail closed");
    assert!(matches!(error, AdapterError::InvalidPayload { .. }));
}

#[test]
fn non_responses_protocols_reduce_special_tools_reversibly() {
    let special_tools = vec![
        ToolSpec::ToolSearch {
            execution: "client".to_string(),
            description: "Search deferred tools".to_string(),
            parameters: JsonSchema::object(
                BTreeMap::from([(
                    "query".to_string(),
                    JsonSchema::string(Some("Search query".to_string())),
                )]),
                Some(vec!["query".to_string()]),
                Some(AdditionalProperties::Boolean(false)),
            ),
        },
        ToolSpec::Freeform(FreeformTool {
            name: "apply_patch".to_string(),
            description: "Apply a patch".to_string(),
            format: FreeformToolFormat {
                r#type: "grammar".to_string(),
                syntax: "lark".to_string(),
                definition: "start: /.+/".to_string(),
            },
        }),
        ToolSpec::WebSearch {
            external_web_access: Some(true),
            indexed_web_access: None,
            filters: None,
            user_location: None,
            search_context_size: None,
            search_content_types: None,
        },
    ];
    let request =
        request_with_tools(vec![user_message("probe")], special_tools).expect("canonical request");

    let chat = OpenAiCompatibleAdapter
        .encode_request(&request)
        .expect("chat reduction");
    let tools = chat.body["tools"].as_array().expect("chat tools");
    assert_eq!(tools.len(), 2);
    assert!(tools.iter().any(|tool| {
        tool["function"]["name"] == "tool_search" && tool["function"]["strict"] == false
    }));
    assert!(tools.iter().any(|tool| {
        tool["function"]["name"] == "apply_patch"
            && tool["function"]["parameters"]["required"] == json!(["input"])
    }));

    let anthropic = AnthropicMessagesAdapter
        .encode_request(&request)
        .expect("Anthropic reduction");
    assert_eq!(anthropic.body["tools"].as_array().map(Vec::len), Some(2));
    let gemini = GeminiNativeAdapter
        .encode_request(&request)
        .expect("Gemini reduction");
    assert_eq!(
        gemini.body["tools"][0]["functionDeclarations"]
            .as_array()
            .map(Vec::len),
        Some(2)
    );
}

#[test]
fn anthropic_maps_tool_use_tool_result_and_cache_usage() {
    let adapter = AnthropicMessagesAdapter;
    let request =
        canonical_request(tool_round_trip_input()).expect("build canonical Anthropic request");
    let encoded = adapter
        .encode_request(&request)
        .expect("encode Anthropic request");
    assert_eq!(encoded.path, "messages");
    assert_eq!(encoded.body["tools"][0]["name"], "read_file");
    let messages = encoded.body["messages"].as_array().expect("messages array");
    assert!(messages.iter().any(|message| {
        message["content"].as_array().is_some_and(|blocks| {
            blocks
                .iter()
                .any(|block| block["type"] == "tool_result" && block["tool_use_id"] == "call_123")
        })
    }));

    let mut state = AnthropicDecodeState::default();
    adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "type": "message_start",
                "message": {
                    "id": "msg_anthropic",
                    "usage": {
                        "input_tokens": 6,
                        "cache_read_input_tokens": 4,
                        "cache_creation_input_tokens": 2
                    }
                }
            })),
        )
        .expect("message start");
    adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {}}
            })),
        )
        .expect("tool start");
    let delta = adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{\"path\":\"src/lib.rs\"}"}
            })),
        )
        .expect("tool delta");
    assert!(delta.iter().any(|event| matches!(
        event,
        ResponseEvent::ToolCallInputDelta { call_id: Some(call_id), .. }
            if call_id == "toolu_1"
    )));
    adapter
        .decode_event(
            &mut state,
            json_event(json!({"type": "content_block_stop", "index": 0})),
        )
        .expect("tool stop");
    adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 3}
            })),
        )
        .expect("message delta");
    let completed = adapter
        .decode_event(&mut state, json_event(json!({"type": "message_stop"})))
        .expect("message stop");
    assert!(completed.iter().any(|event| matches!(
        event,
        ResponseEvent::Completed {
            token_usage: Some(usage),
            end_turn: Some(false),
            ..
        } if usage.input_tokens == 12
            && usage.cached_input_tokens == 4
            && usage.cache_write_input_tokens == 2
            && usage.output_tokens == 3
    )));
}

#[test]
fn gemini_maps_function_calls_and_generates_a_stable_call_id() {
    let adapter = GeminiNativeAdapter;
    let request =
        canonical_request(tool_round_trip_input()).expect("build canonical Gemini request");
    let encoded = adapter
        .encode_request(&request)
        .expect("encode Gemini request");
    assert_eq!(
        encoded.path,
        "models/deepseek-v4-flash:streamGenerateContent?alt=sse"
    );
    assert_eq!(
        encoded.body["tools"][0]["functionDeclarations"][0]["name"],
        "read_file"
    );
    assert!(encoded.body["contents"].as_array().is_some_and(|contents| {
        contents.iter().any(|content| {
            content["parts"].as_array().is_some_and(|parts| {
                parts
                    .iter()
                    .any(|part| part["functionResponse"]["name"] == "read_file")
            })
        })
    }));

    let mut state = GeminiDecodeState::default();
    let events = adapter
        .decode_event(
            &mut state,
            json_event(json!({
                "responseId": "gemini_1",
                "candidates": [{
                    "content": {"parts": [{"functionCall": {"name": "read_file", "args": {"path": "src/lib.rs"}}}]},
                    "finishReason": "STOP"
                }],
                "usageMetadata": {
                    "promptTokenCount": 8,
                    "cachedContentTokenCount": 3,
                    "candidatesTokenCount": 2,
                    "totalTokenCount": 10
                }
            })),
        )
        .expect("decode Gemini call");
    assert!(events.iter().any(|event| matches!(
        event,
        ResponseEvent::OutputItemDone(ResponseItem::FunctionCall {
            call_id,
            arguments,
            ..
        }) if call_id == "call_gemini_0_0" && arguments == r#"{"path":"src/lib.rs"}"#
    )));
    let completed = adapter
        .decode_event(&mut state, WireEvent::Done)
        .expect("finish Gemini stream");
    assert!(matches!(
        completed.as_slice(),
        [ResponseEvent::Completed {
            token_usage: Some(usage),
            end_turn: Some(false),
            ..
        }] if usage.cached_input_tokens == 3 && usage.total_tokens == 10
    ));
}

#[test]
fn unknown_tool_events_fail_closed() {
    let adapter = OpenAiResponsesAdapter;
    let error = adapter
        .decode_event(
            &mut ResponsesDecodeState::default(),
            json_event(json!({"type": "response.mystery_tool.delta"})),
        )
        .expect_err("unknown tool event must fail");
    assert!(matches!(error, AdapterError::UnsupportedEvent { .. }));
}
