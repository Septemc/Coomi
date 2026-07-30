use anyhow::Context;
use anyhow::Result;
use coomi_provider_adapters::EmbeddedProviderGateway;
use coomi_provider_adapters::HttpProviderTransport;
use coomi_provider_adapters::ProviderRegistry;
use core_test_support::responses::start_mock_server;
use core_test_support::skip_if_no_network;
use core_test_support::test_codex::test_codex;
use serde_json::Value;
use serde_json::json;
use std::fmt::Write as _;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use wiremock::Mock;
use wiremock::MockServer;
use wiremock::Request;
use wiremock::Respond;
use wiremock::ResponseTemplate;
use wiremock::matchers::method;
use wiremock::matchers::path;

const TEST_THREAD_STACK_SIZE: usize = 8 * 1024 * 1024;

struct SequenceResponder {
    next: AtomicUsize,
    responses: Vec<String>,
}

impl Respond for SequenceResponder {
    fn respond(&self, _request: &Request) -> ResponseTemplate {
        let index = self.next.fetch_add(1, Ordering::SeqCst);
        let body = self.responses.get(index).cloned().unwrap_or_else(|| {
            chat_sse(vec![json!({
                "error": {"message": "unexpected extra model request"}
            })])
        });
        ResponseTemplate::new(200)
            .insert_header("content-type", "text/event-stream")
            .set_body_raw(body, "text/event-stream")
    }
}

#[test]
fn openai_compatible_gateway_completes_a_real_core_tool_loop() -> Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_stack_size(TEST_THREAD_STACK_SIZE)
        .enable_all()
        .build()?;

    runtime.block_on(async {
        skip_if_no_network!(Ok(()));

        let provider_server = MockServer::start().await;
        let command_arguments = json!({"command": "echo gateway-tool-ok"}).to_string();
        let tool_call = chat_sse(vec![json!({
            "id": "chat-gateway-tool",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "gateway-call-1",
                        "function": {
                            "name": "shell_command",
                            "arguments": command_arguments,
                        }
                    }]
                },
                "finish_reason": "tool_calls"
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
        })]);
        let final_answer = chat_sse(vec![json!({
            "id": "chat-gateway-final",
            "choices": [{
                "delta": {"content": "GATEWAY_TOOL_LOOP_OK"},
                "finish_reason": "stop"
            }],
            "usage": {"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34}
        })]);
        Mock::given(method("POST"))
            .and(path("/v1/chat/completions"))
            .respond_with(SequenceResponder {
                next: AtomicUsize::new(0),
                responses: vec![tool_call, final_answer],
            })
            .expect(2)
            .mount(&provider_server)
            .await;

        let registry = mock_registry(&provider_server)?;
        let transport = HttpProviderTransport::new(registry.active_provider()?.clone())?;
        let gateway = EmbeddedProviderGateway::start(transport).await?;
        let gateway_base_url = gateway.base_url().to_string();

        let unused_responses_server = start_mock_server().await;
        let test = test_codex()
            .with_model("deepseek-v4-flash")
            .with_config(move |config| configure_gateway_provider(config, gateway_base_url))
            .build(&unused_responses_server)
            .await?;
        assert_eq!(
            test.config.model_provider.base_url.as_deref(),
            Some(gateway.base_url().as_str())
        );

        test.submit_turn("Run the gateway tool-loop probe.").await?;

        let requests = provider_server
            .received_requests()
            .await
            .context("wiremock request recording is disabled")?;
        assert_eq!(
            requests.len(),
            2,
            "gateway requests={}, last error={:?}",
            gateway.request_count(),
            gateway.last_error()
        );
        let first: Value = serde_json::from_slice(&requests[0].body)?;
        assert_eq!(first["model"], "deepseek-v4-flash");
        assert!(first["tools"].as_array().is_some_and(|tools| {
            tools
                .iter()
                .any(|tool| tool["function"]["name"] == "shell_command")
        }));

        let second: Value = serde_json::from_slice(&requests[1].body)?;
        assert!(second["messages"].as_array().is_some_and(|messages| {
            messages.iter().any(|message| {
                message["role"] == "tool"
                    && message["tool_call_id"] == "gateway-call-1"
                    && message["content"]
                        .as_str()
                        .is_some_and(|content| content.contains("gateway-tool-ok"))
            })
        }));

        gateway.shutdown().await;
        Ok(())
    })
}

#[test]
fn openai_compatible_reasoning_stream_completes_a_core_turn() -> Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_stack_size(TEST_THREAD_STACK_SIZE)
        .enable_all()
        .build()?;

    runtime.block_on(async {
        skip_if_no_network!(Ok(()));

        let provider_server = MockServer::start().await;
        let response = chat_sse(vec![
            json!({
                "id": "chat-gateway-reasoning",
                "choices": [{
                    "delta": {"reasoning_content": "Verify the requested literal."},
                    "finish_reason": null
                }]
            }),
            json!({
                "id": "chat-gateway-reasoning",
                "choices": [{
                    "delta": {"content": "GATEWAY_REASONING_OK"},
                    "finish_reason": "stop"
                }],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
            }),
        ]);
        Mock::given(method("POST"))
            .and(path("/v1/chat/completions"))
            .respond_with(ResponseTemplate::new(200).set_body_raw(response, "text/event-stream"))
            .expect(1)
            .mount(&provider_server)
            .await;

        let registry = mock_registry(&provider_server)?;
        let gateway = EmbeddedProviderGateway::start(HttpProviderTransport::new(
            registry.active_provider()?.clone(),
        )?)
        .await?;
        let gateway_base_url = gateway.base_url().to_string();
        let unused_responses_server = start_mock_server().await;
        let test = test_codex()
            .with_model("deepseek-v4-flash")
            .with_config(move |config| configure_gateway_provider(config, gateway_base_url))
            .build(&unused_responses_server)
            .await?;

        test.submit_turn("Return the reasoning-stream probe literal.")
            .await?;
        assert_eq!(gateway.request_count(), 1);
        assert_eq!(gateway.last_error(), None);

        gateway.shutdown().await;
        Ok(())
    })
}

#[test]
fn gateway_rejects_invalid_tool_arguments_before_side_effects() -> Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_stack_size(TEST_THREAD_STACK_SIZE)
        .enable_all()
        .build()?;

    runtime.block_on(async {
        skip_if_no_network!(Ok(()));

        let provider_server = MockServer::start().await;
        let sentinel = std::env::temp_dir().join(format!(
            "coomi-schema-guard-{}-{}.txt",
            std::process::id(),
            provider_server.address().port()
        ));
        let command = format!(
            "Set-Content -LiteralPath '{}' -Value 'must-not-run'",
            sentinel.display()
        );
        let invalid_call = chat_sse(vec![json!({
            "id": "chat-invalid-tool",
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "invalid-call-1",
                        "function": {
                            "name": "shell_command",
                            "arguments": json!({
                                "command": command,
                                "provider_invented_field": true
                            }).to_string(),
                        }
                    }]
                },
                "finish_reason": "tool_calls"
            }]
        })]);
        let final_answer = chat_sse(vec![json!({
            "id": "chat-schema-final",
            "choices": [{
                "delta": {"content": "SCHEMA_GUARD_OK"},
                "finish_reason": "stop"
            }]
        })]);
        Mock::given(method("POST"))
            .and(path("/v1/chat/completions"))
            .respond_with(SequenceResponder {
                next: AtomicUsize::new(0),
                responses: vec![invalid_call, final_answer],
            })
            .expect(2)
            .mount(&provider_server)
            .await;

        let registry = mock_registry(&provider_server)?;
        let gateway = EmbeddedProviderGateway::start(HttpProviderTransport::new(
            registry.active_provider()?.clone(),
        )?)
        .await?;
        let gateway_base_url = gateway.base_url().to_string();
        let unused_responses_server = start_mock_server().await;
        let test = test_codex()
            .with_model("deepseek-v4-flash")
            .with_config(move |config| configure_gateway_provider(config, gateway_base_url))
            .build(&unused_responses_server)
            .await?;

        test.submit_turn("Run the schema guard probe.").await?;

        assert!(
            !sentinel.exists(),
            "invalid tool arguments caused a side effect"
        );
        let requests = provider_server
            .received_requests()
            .await
            .context("wiremock request recording is disabled")?;
        assert_eq!(requests.len(), 2);
        let second: Value = serde_json::from_slice(&requests[1].body)?;
        assert!(second["messages"].as_array().is_some_and(|messages| {
            messages.iter().any(|message| {
                message["role"] == "tool"
                    && message["tool_call_id"] == "invalid-call-1"
                    && message["content"]
                        .as_str()
                        .is_some_and(|content| content.contains("unexpected property"))
            })
        }));

        gateway.shutdown().await;
        Ok(())
    })
}

fn mock_registry(provider_server: &MockServer) -> Result<ProviderRegistry> {
    ProviderRegistry::from_json(&format!(
        r#"{{
            "version": 1,
            "active": "opencode-go",
            "providers": {{
                "opencode-go": {{
                    "type": "openai_compatible",
                    "display": "OpenCode Go Mock",
                    "api_key": "test-key",
                    "model": "deepseek-v4-flash",
                    "base_url": "{}/v1"
                }}
            }}
        }}"#,
        provider_server.uri()
    ))
    .map_err(Into::into)
}

fn configure_gateway_provider(config: &mut codex_core::config::Config, gateway_base_url: String) {
    config.model_provider.name = "coomi-runtime".to_string();
    config.model_provider.base_url = Some(gateway_base_url);
    config.model_provider.env_key = Some("PATH".to_string());
    config.model_provider.requires_openai_auth = false;
    config.model_provider.supports_websockets = false;
    config.model_provider.request_max_retries = Some(0);
    config.model_provider.stream_max_retries = Some(0);
}

fn chat_sse(events: Vec<Value>) -> String {
    let mut body = String::new();
    for event in events {
        let _ = writeln!(&mut body, "data: {event}\n");
    }
    body.push_str("data: [DONE]\n\n");
    body
}
