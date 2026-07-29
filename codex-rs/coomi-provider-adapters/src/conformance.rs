use crate::AdapterError;
use crate::CanonicalTransport;
use crate::HttpProviderTransport;
use crate::ResponseEvent;
use crate::ResponseItem;
use crate::ResponsesApiRequest;
use codex_protocol::models::ContentItem;
use codex_protocol::models::FunctionCallOutputPayload;
use codex_protocol::protocol::TokenUsage;
use codex_tools::AdditionalProperties;
use codex_tools::JsonSchema;
use codex_tools::ResponsesApiTool;
use codex_tools::ToolSpec;
use codex_tools::create_tools_raw_json_for_responses_api;
use futures::StreamExt;
use serde::Serialize;
use serde_json::Value;
use std::collections::BTreeMap;
use std::collections::BTreeSet;

const PROBE_VALUE: &str = "coomi-provider-probe";

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub enum ConformanceCase {
    C01SingleFunctionCall,
    C02StreamedArguments,
    C03ToolResultContinuation,
    C04ParallelCalls,
    C05SchemaBoundaries,
    C06ServerStrictness,
    C07ReasoningInterleave,
    C08InterruptedStream,
    C09UsageAndFinish,
    C10LimitsAndErrors,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ConformanceStatus {
    Passed,
    Failed,
    NotRun,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ConformanceResult {
    pub case: ConformanceCase,
    pub status: ConformanceStatus,
    pub detail: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct ConformanceReport {
    pub provider_id: String,
    pub model: String,
    pub protocol: crate::WireProtocol,
    pub results: Vec<ConformanceResult>,
    pub input_tokens: i64,
    pub cached_input_tokens: i64,
    pub output_tokens: i64,
    pub total_tokens: i64,
}

impl ConformanceReport {
    pub fn required_tool_loop_passed(&self) -> bool {
        self.results.iter().all(|result| {
            !matches!(
                result.case,
                ConformanceCase::C01SingleFunctionCall
                    | ConformanceCase::C02StreamedArguments
                    | ConformanceCase::C03ToolResultContinuation
            ) || result.status == ConformanceStatus::Passed
        })
    }
}

#[derive(Debug, Default)]
struct CollectedResponse {
    done_items: Vec<ResponseItem>,
    argument_deltas: Vec<String>,
    usage: Option<TokenUsage>,
    end_turn: Option<bool>,
    reasoning_deltas: usize,
}

pub async fn run_basic_conformance(
    transport: &HttpProviderTransport,
    model: &str,
) -> Result<ConformanceReport, AdapterError> {
    let first_input = vec![user_message(
        "Call `coomi_probe_echo` exactly once with value `coomi-provider-probe`. After the tool result arrives, answer with `PROBE_OK` followed by that value.",
    )];
    let first = collect(transport, &probe_request(model, first_input.clone())?).await?;
    let calls = first
        .done_items
        .iter()
        .filter_map(|item| match item {
            ResponseItem::FunctionCall {
                name,
                arguments,
                call_id,
                ..
            } => Some((name.clone(), arguments.clone(), call_id.clone())),
            _ => None,
        })
        .collect::<Vec<_>>();

    let c01 = if calls.len() != 1 {
        failed(
            ConformanceCase::C01SingleFunctionCall,
            format!("expected one tool call, received {}", calls.len()),
        )
    } else {
        let (name, arguments, call_id) = &calls[0];
        let parsed = serde_json::from_str::<Value>(arguments);
        if name == "coomi_probe_echo"
            && !call_id.trim().is_empty()
            && parsed
                .as_ref()
                .ok()
                .and_then(|value| value.get("value"))
                .and_then(Value::as_str)
                == Some(PROBE_VALUE)
        {
            passed(
                ConformanceCase::C01SingleFunctionCall,
                "native function call, JSON arguments, and non-empty call_id verified",
            )
        } else {
            failed(
                ConformanceCase::C01SingleFunctionCall,
                format!(
                    "unexpected call name/arguments/call_id: name={name}, call_id_present={}",
                    !call_id.trim().is_empty()
                ),
            )
        }
    };

    let c02 = calls.first().map_or_else(
        || {
            failed(
                ConformanceCase::C02StreamedArguments,
                "no completed tool call was available for delta comparison",
            )
        },
        |(_, arguments, _)| {
            let streamed = first.argument_deltas.concat();
            if !first.argument_deltas.is_empty() && streamed == *arguments {
                passed(
                    ConformanceCase::C02StreamedArguments,
                    format!(
                        "{} ordered argument fragment(s) reconstructed the final JSON",
                        first.argument_deltas.len()
                    ),
                )
            } else {
                failed(
                    ConformanceCase::C02StreamedArguments,
                    "streamed argument fragments did not reconstruct the completed call",
                )
            }
        },
    );

    let mut second = None;
    if let Some((name, arguments, call_id)) = calls.first() {
        let mut continuation = first_input;
        continuation.push(ResponseItem::FunctionCall {
            id: None,
            name: name.clone(),
            namespace: None,
            arguments: arguments.clone(),
            encrypted_function_args: None,
            call_id: call_id.clone(),
            internal_chat_message_metadata_passthrough: None,
        });
        continuation.push(ResponseItem::FunctionCallOutput {
            id: None,
            call_id: call_id.clone(),
            output: FunctionCallOutputPayload::from_text(PROBE_VALUE.to_string()),
            internal_chat_message_metadata_passthrough: None,
        });
        second = Some(collect(transport, &probe_request(model, continuation)?).await?);
    }

    let c03 = match second.as_ref() {
        Some(response) => {
            let text = response
                .done_items
                .iter()
                .filter_map(|item| match item {
                    ResponseItem::Message { content, .. } => Some(
                        content
                            .iter()
                            .filter_map(|item| match item {
                                ContentItem::OutputText { text } => Some(text.as_str()),
                                _ => None,
                            })
                            .collect::<String>(),
                    ),
                    _ => None,
                })
                .collect::<String>();
            let repeated_call = response
                .done_items
                .iter()
                .any(|item| matches!(item, ResponseItem::FunctionCall { .. }));
            if text.contains("PROBE_OK") && text.contains(PROBE_VALUE) && !repeated_call {
                passed(
                    ConformanceCase::C03ToolResultContinuation,
                    "tool output was accepted and the model produced the terminal answer",
                )
            } else {
                failed(
                    ConformanceCase::C03ToolResultContinuation,
                    "provider did not produce the expected terminal answer after tool output",
                )
            }
        }
        None => failed(
            ConformanceCase::C03ToolResultContinuation,
            "first turn did not produce a tool call",
        ),
    };

    let usage = sum_usage(
        first.usage.as_ref(),
        second.as_ref().and_then(|value| value.usage.as_ref()),
    );
    let c09 = if usage.total_tokens > 0
        && first.end_turn == Some(false)
        && second
            .as_ref()
            .is_some_and(|value| value.end_turn == Some(true))
    {
        passed(
            ConformanceCase::C09UsageAndFinish,
            "usage and tool/final finish semantics were mapped",
        )
    } else {
        failed(
            ConformanceCase::C09UsageAndFinish,
            "usage or finish semantics were incomplete",
        )
    };

    let mut results = vec![c01, c02, c03];
    for case in [
        ConformanceCase::C04ParallelCalls,
        ConformanceCase::C05SchemaBoundaries,
        ConformanceCase::C06ServerStrictness,
        ConformanceCase::C07ReasoningInterleave,
        ConformanceCase::C08InterruptedStream,
    ] {
        results.push(not_run(case, "not part of the basic no-side-effect probe"));
    }
    results.push(c09);
    results.push(not_run(
        ConformanceCase::C10LimitsAndErrors,
        "not part of the basic no-side-effect probe",
    ));

    Ok(ConformanceReport {
        provider_id: transport.provider_id().to_string(),
        model: model.to_string(),
        protocol: transport.capabilities().wire_protocol,
        results,
        input_tokens: usage.input_tokens,
        cached_input_tokens: usage.cached_input_tokens,
        output_tokens: usage.output_tokens,
        total_tokens: usage.total_tokens,
    })
}

pub async fn run_full_conformance(
    transport: &HttpProviderTransport,
    model: &str,
) -> Result<ConformanceReport, AdapterError> {
    let mut report = run_basic_conformance(transport, model).await?;

    let (c04, usage) = probe_parallel_calls(transport, model).await?;
    replace_result(&mut report, c04);
    append_usage(&mut report, &usage);

    let (c05, usage) = probe_schema_boundaries(transport, model).await?;
    replace_result(&mut report, c05);
    append_usage(&mut report, &usage);

    let (c06, usage) = probe_server_strictness(transport, model).await?;
    replace_result(&mut report, c06);
    append_usage(&mut report, &usage);

    let (c07, usage) = probe_reasoning_interleave(transport, model).await?;
    replace_result(&mut report, c07);
    append_usage(&mut report, &usage);

    replace_result(&mut report, probe_error_response(transport, model).await);
    Ok(report)
}

async fn probe_parallel_calls(
    transport: &HttpProviderTransport,
    model: &str,
) -> Result<(ConformanceResult, TokenUsage), AdapterError> {
    let tools = vec![
        string_tool("coomi_probe_alpha", "value", false),
        string_tool("coomi_probe_beta", "value", false),
    ];
    let request = request_with_tools(
        model,
        vec![user_message(
            "Call both `coomi_probe_alpha` with value `alpha` and `coomi_probe_beta` with value `beta` in the same response.",
        )],
        tools,
        true,
    )?;
    let response = collect(transport, &request).await?;
    let calls = response
        .done_items
        .iter()
        .filter_map(|item| match item {
            ResponseItem::FunctionCall {
                name,
                call_id,
                arguments,
                ..
            } => Some((name.as_str(), call_id.as_str(), arguments.as_str())),
            _ => None,
        })
        .collect::<Vec<_>>();
    let names = calls
        .iter()
        .map(|(name, _, _)| *name)
        .collect::<BTreeSet<_>>();
    let call_ids = calls
        .iter()
        .map(|(_, call_id, _)| *call_id)
        .collect::<BTreeSet<_>>();
    let valid_arguments = calls.iter().all(|(name, _, arguments)| {
        serde_json::from_str::<Value>(arguments)
            .ok()
            .and_then(|value| {
                value
                    .get("value")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            })
            .is_some_and(|value| {
                (*name == "coomi_probe_alpha" && value == "alpha")
                    || (*name == "coomi_probe_beta" && value == "beta")
            })
    });
    let result = if names == BTreeSet::from(["coomi_probe_alpha", "coomi_probe_beta"])
        && call_ids.len() == 2
        && valid_arguments
    {
        passed(
            ConformanceCase::C04ParallelCalls,
            "two parallel calls preserved distinct names, arguments, and call_ids",
        )
    } else {
        failed(
            ConformanceCase::C04ParallelCalls,
            format!(
                "expected two distinct parallel calls; observed calls={} names={} call_ids={}",
                calls.len(),
                names.len(),
                call_ids.len()
            ),
        )
    };
    Ok((result, response.usage.unwrap_or_default()))
}

async fn probe_schema_boundaries(
    transport: &HttpProviderTransport,
    model: &str,
) -> Result<(ConformanceResult, TokenUsage), AdapterError> {
    let nested = JsonSchema::object(
        BTreeMap::from([
            (
                "count".to_string(),
                JsonSchema::integer(Some("Exact count".to_string())),
            ),
            (
                "enabled".to_string(),
                JsonSchema::boolean(Some("Feature flag".to_string())),
            ),
        ]),
        Some(vec!["count".to_string(), "enabled".to_string()]),
        Some(AdditionalProperties::Boolean(false)),
    );
    let schema = JsonSchema::object(
        BTreeMap::from([
            (
                "mode".to_string(),
                JsonSchema::string_enum(
                    vec![
                        Value::String("alpha".to_string()),
                        Value::String("beta".to_string()),
                    ],
                    Some("Execution mode".to_string()),
                ),
            ),
            ("payload".to_string(), nested),
        ]),
        Some(vec!["mode".to_string(), "payload".to_string()]),
        Some(AdditionalProperties::Boolean(false)),
    );
    let request = request_with_tools(
        model,
        vec![user_message(
            "Call `coomi_probe_schema` with mode `beta` and payload containing count 2 and enabled true.",
        )],
        vec![function_tool("coomi_probe_schema", schema, true)],
        false,
    )?;
    let response = collect(transport, &request).await?;
    let arguments = only_call_arguments(&response, "coomi_probe_schema");
    let valid = arguments.as_ref().is_some_and(|arguments| {
        arguments.get("mode").and_then(Value::as_str) == Some("beta")
            && arguments
                .get("payload")
                .and_then(|payload| payload.get("count"))
                .and_then(Value::as_i64)
                == Some(2)
            && arguments
                .get("payload")
                .and_then(|payload| payload.get("enabled"))
                .and_then(Value::as_bool)
                == Some(true)
            && arguments.as_object().is_some_and(|value| value.len() == 2)
            && arguments
                .get("payload")
                .and_then(Value::as_object)
                .is_some_and(|value| value.len() == 2)
    });
    let result = if valid {
        passed(
            ConformanceCase::C05SchemaBoundaries,
            "nested object, enum, required fields, and additionalProperties boundary passed",
        )
    } else {
        failed(
            ConformanceCase::C05SchemaBoundaries,
            "provider did not honor the nested schema boundary",
        )
    };
    Ok((result, response.usage.unwrap_or_default()))
}

async fn probe_server_strictness(
    transport: &HttpProviderTransport,
    model: &str,
) -> Result<(ConformanceResult, TokenUsage), AdapterError> {
    let schema = JsonSchema::object(
        BTreeMap::from([(
            "value".to_string(),
            JsonSchema::string_enum(
                vec![Value::String("strict-ok".to_string())],
                Some("Only strict-ok is valid".to_string()),
            ),
        )]),
        Some(vec!["value".to_string()]),
        Some(AdditionalProperties::Boolean(false)),
    );
    let request = request_with_tools(
        model,
        vec![user_message(
            "Attempt to call `coomi_probe_strict` with value `strict-invalid`, even though its schema only permits another value.",
        )],
        vec![function_tool("coomi_probe_strict", schema, true)],
        false,
    )?;
    let response = collect(transport, &request).await?;
    let arguments = only_call_arguments(&response, "coomi_probe_strict");
    let result = match arguments
        .as_ref()
        .and_then(|arguments| arguments.get("value"))
        .and_then(Value::as_str)
    {
        Some("strict-ok") => passed(
            ConformanceCase::C06ServerStrictness,
            "adversarial invalid enum request was constrained to the declared value",
        ),
        Some(other) => failed(
            ConformanceCase::C06ServerStrictness,
            format!("provider emitted schema-invalid enum value `{other}`"),
        ),
        None => not_run(
            ConformanceCase::C06ServerStrictness,
            "provider declined the adversarial call; server enforcement remains inconclusive",
        ),
    };
    Ok((result, response.usage.unwrap_or_default()))
}

async fn probe_reasoning_interleave(
    transport: &HttpProviderTransport,
    model: &str,
) -> Result<(ConformanceResult, TokenUsage), AdapterError> {
    let request = request_with_tools(
        model,
        vec![user_message(
            "Reason briefly, then call `coomi_probe_reasoning` once with value `reasoning-ok`.",
        )],
        vec![string_tool("coomi_probe_reasoning", "value", true)],
        false,
    )?;
    let response = collect(transport, &request).await?;
    let has_call = only_call_arguments(&response, "coomi_probe_reasoning").is_some();
    let result = if has_call && response.reasoning_deltas > 0 {
        passed(
            ConformanceCase::C07ReasoningInterleave,
            format!(
                "{} reasoning fragment(s) were separated from the native tool call",
                response.reasoning_deltas
            ),
        )
    } else if has_call {
        not_run(
            ConformanceCase::C07ReasoningInterleave,
            "tool call passed, but this model emitted no reasoning stream to test interleaving",
        )
    } else {
        failed(
            ConformanceCase::C07ReasoningInterleave,
            "reasoning probe did not produce the required native tool call",
        )
    };
    Ok((result, response.usage.unwrap_or_default()))
}

async fn probe_error_response(transport: &HttpProviderTransport, model: &str) -> ConformanceResult {
    let invalid_model = format!("{model}-coomi-invalid-model");
    let request = request_with_tools(
        &invalid_model,
        vec![user_message("Return the word probe.")],
        Vec::new(),
        false,
    );
    let Ok(request) = request else {
        return failed(
            ConformanceCase::C10LimitsAndErrors,
            "failed to construct the invalid-model error probe",
        );
    };
    match collect(transport, &request).await {
        Err(AdapterError::HttpStatus { status, .. }) => passed(
            ConformanceCase::C10LimitsAndErrors,
            format!("invalid model produced explicit HTTP {status}"),
        ),
        Err(error) => failed(
            ConformanceCase::C10LimitsAndErrors,
            format!("invalid model produced an unmapped error: {error}"),
        ),
        Ok(_) => failed(
            ConformanceCase::C10LimitsAndErrors,
            "provider silently accepted the deliberately invalid model id",
        ),
    }
}

async fn collect(
    transport: &HttpProviderTransport,
    request: &ResponsesApiRequest,
) -> Result<CollectedResponse, AdapterError> {
    let mut response = CollectedResponse::default();
    let mut stream = transport.stream(request).await?;
    while let Some(event) = stream.next().await {
        match event? {
            ResponseEvent::OutputItemDone(item) => response.done_items.push(item),
            ResponseEvent::ToolCallInputDelta { delta, .. } => {
                response.argument_deltas.push(delta);
            }
            ResponseEvent::Completed {
                token_usage,
                end_turn,
                ..
            } => {
                response.usage = token_usage;
                response.end_turn = end_turn;
            }
            ResponseEvent::ReasoningContentDelta { .. }
            | ResponseEvent::ReasoningSummaryDelta { .. } => {
                response.reasoning_deltas += 1;
            }
            _ => {}
        }
    }
    Ok(response)
}

fn probe_request(
    model: &str,
    input: Vec<ResponseItem>,
) -> Result<ResponsesApiRequest, AdapterError> {
    request_with_tools(
        model,
        input,
        vec![string_tool("coomi_probe_echo", "value", true)],
        false,
    )
}

fn request_with_tools(
    model: &str,
    input: Vec<ResponseItem>,
    tools: Vec<ToolSpec>,
    parallel_tool_calls: bool,
) -> Result<ResponsesApiRequest, AdapterError> {
    Ok(ResponsesApiRequest {
        model: model.to_string(),
        instructions: "You are a deterministic provider conformance probe. Use only the declared native function tool and never invent tool syntax in text."
            .to_string(),
        input,
        tools: Some(create_tools_raw_json_for_responses_api(&tools)?.into()),
        tool_choice: "auto".to_string(),
        parallel_tool_calls,
        reasoning: None,
        store: false,
        stream: true,
        stream_options: None,
        include: Vec::new(),
        service_tier: None,
        prompt_cache_key: Some("coomi-provider-conformance".to_string()),
        text: None,
        client_metadata: None,
    })
}

fn string_tool(name: &str, property: &str, strict: bool) -> ToolSpec {
    function_tool(
        name,
        JsonSchema::object(
            BTreeMap::from([(
                property.to_string(),
                JsonSchema::string(Some(format!("Value for {property}"))),
            )]),
            Some(vec![property.to_string()]),
            Some(AdditionalProperties::Boolean(false)),
        ),
        strict,
    )
}

fn function_tool(name: &str, parameters: JsonSchema, strict: bool) -> ToolSpec {
    ToolSpec::Function(ResponsesApiTool {
        name: name.to_string(),
        description: format!("Side-effect-free Coomi provider probe `{name}`."),
        strict,
        defer_loading: None,
        parameters,
        output_schema: None,
    })
}

fn only_call_arguments(response: &CollectedResponse, name: &str) -> Option<Value> {
    let mut calls = response.done_items.iter().filter_map(|item| match item {
        ResponseItem::FunctionCall {
            name: call_name,
            arguments,
            ..
        } if call_name == name => serde_json::from_str::<Value>(arguments).ok(),
        _ => None,
    });
    let first = calls.next()?;
    calls.next().is_none().then_some(first)
}

fn replace_result(report: &mut ConformanceReport, result: ConformanceResult) {
    if let Some(existing) = report
        .results
        .iter_mut()
        .find(|existing| existing.case == result.case)
    {
        *existing = result;
    } else {
        report.results.push(result);
    }
}

fn append_usage(report: &mut ConformanceReport, usage: &TokenUsage) {
    report.input_tokens += usage.input_tokens;
    report.cached_input_tokens += usage.cached_input_tokens;
    report.output_tokens += usage.output_tokens;
    report.total_tokens += usage.total_tokens;
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

fn sum_usage(first: Option<&TokenUsage>, second: Option<&TokenUsage>) -> TokenUsage {
    let mut total = TokenUsage::default();
    for usage in [first, second].into_iter().flatten() {
        total.input_tokens += usage.input_tokens;
        total.cached_input_tokens += usage.cached_input_tokens;
        total.cache_write_input_tokens += usage.cache_write_input_tokens;
        total.output_tokens += usage.output_tokens;
        total.reasoning_output_tokens += usage.reasoning_output_tokens;
        total.total_tokens += usage.total_tokens;
    }
    total
}

fn passed(case: ConformanceCase, detail: impl Into<String>) -> ConformanceResult {
    ConformanceResult {
        case,
        status: ConformanceStatus::Passed,
        detail: detail.into(),
    }
}

fn failed(case: ConformanceCase, detail: impl Into<String>) -> ConformanceResult {
    ConformanceResult {
        case,
        status: ConformanceStatus::Failed,
        detail: detail.into(),
    }
}

fn not_run(case: ConformanceCase, detail: impl Into<String>) -> ConformanceResult {
    ConformanceResult {
        case,
        status: ConformanceStatus::NotRun,
        detail: detail.into(),
    }
}
