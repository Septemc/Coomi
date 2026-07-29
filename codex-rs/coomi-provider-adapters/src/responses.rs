use crate::AdapterError;
use crate::EncodedRequest;
use crate::ResponseEvent;
use crate::ResponseItem;
use crate::ResponsesApiRequest;
use crate::WireAdapter;
use crate::WireEvent;
use crate::WireProtocol;
use crate::common::openai_usage;
use crate::common::request_json;

#[derive(Debug, Default)]
pub struct ResponsesDecodeState {
    response_id: Option<String>,
    completed: bool,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct OpenAiResponsesAdapter;

impl WireAdapter for OpenAiResponsesAdapter {
    type DecodeState = ResponsesDecodeState;

    fn protocol(&self) -> WireProtocol {
        WireProtocol::OpenAiResponses
    }

    fn encode_request(
        &self,
        request: &ResponsesApiRequest,
    ) -> Result<EncodedRequest, AdapterError> {
        Ok(EncodedRequest {
            path: "responses".to_string(),
            body: request_json(request)?,
        })
    }

    fn decode_event(
        &self,
        state: &mut Self::DecodeState,
        event: WireEvent,
    ) -> Result<Vec<ResponseEvent>, AdapterError> {
        let WireEvent::Json { event_type, data } = event else {
            if state.completed {
                return Ok(Vec::new());
            }
            return Err(AdapterError::InvalidPayload {
                protocol: self.protocol(),
                message: "Responses stream ended before `response.completed`".to_string(),
            });
        };
        let event_type = event_type
            .or_else(|| {
                data.get("type")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_owned)
            })
            .unwrap_or_default();
        let protocol = self.protocol();

        match event_type.as_str() {
            "response.created" => {
                state.response_id = data
                    .get("response")
                    .and_then(|response| response.get("id"))
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_owned);
                Ok(vec![ResponseEvent::Created])
            }
            "response.output_item.added" => Ok(vec![ResponseEvent::OutputItemAdded(parse_item(
                protocol, &data,
            )?)]),
            "response.output_item.done" => Ok(vec![ResponseEvent::OutputItemDone(parse_item(
                protocol, &data,
            )?)]),
            "response.output_text.delta" => Ok(vec![ResponseEvent::OutputTextDelta(
                required_string(protocol, &data, "delta")?.to_string(),
            )]),
            "response.function_call_arguments.delta" => {
                Ok(vec![ResponseEvent::ToolCallInputDelta {
                    item_id: required_string(protocol, &data, "item_id")?.to_string(),
                    call_id: data
                        .get("call_id")
                        .and_then(serde_json::Value::as_str)
                        .map(str::to_owned),
                    delta: required_string(protocol, &data, "delta")?.to_string(),
                }])
            }
            "response.completed" => {
                state.completed = true;
                let response = data.get("response").unwrap_or(&data);
                let response_id = response
                    .get("id")
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_owned)
                    .or_else(|| state.response_id.clone())
                    .unwrap_or_default();
                Ok(vec![ResponseEvent::Completed {
                    response_id,
                    token_usage: openai_usage(response.get("usage")),
                    end_turn: response
                        .get("end_turn")
                        .and_then(serde_json::Value::as_bool),
                }])
            }
            "error" | "response.failed" => Err(AdapterError::Provider(data.to_string())),
            unknown if unknown.contains("function_call") || unknown.contains("tool") => {
                Err(AdapterError::UnsupportedEvent {
                    protocol,
                    event_type: unknown.to_string(),
                })
            }
            _ => Ok(Vec::new()),
        }
    }
}

fn parse_item(
    protocol: WireProtocol,
    data: &serde_json::Value,
) -> Result<ResponseItem, AdapterError> {
    let item = data
        .get("item")
        .ok_or_else(|| AdapterError::InvalidPayload {
            protocol,
            message: "output item event is missing `item`".to_string(),
        })?;
    serde_json::from_value(item.clone()).map_err(AdapterError::from)
}

fn required_string<'a>(
    protocol: WireProtocol,
    data: &'a serde_json::Value,
    field: &str,
) -> Result<&'a str, AdapterError> {
    data.get(field)
        .and_then(serde_json::Value::as_str)
        .ok_or_else(|| AdapterError::InvalidPayload {
            protocol,
            message: format!("event is missing string field `{field}`"),
        })
}
