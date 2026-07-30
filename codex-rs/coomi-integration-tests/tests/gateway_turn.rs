use anyhow::Context;
use anyhow::Result;
use codex_core::CodexThread;
use codex_core::ForkSnapshot;
use codex_core::NewThread;
use codex_protocol::items::TurnItem;
use codex_protocol::protocol::EventMsg;
use codex_protocol::protocol::ItemCompletedEvent;
use codex_protocol::protocol::ItemStartedEvent;
use codex_protocol::protocol::Op;
use codex_protocol::protocol::TurnAbortReason;
use codex_protocol::user_input::UserInput;
use coomi_provider_adapters::EmbeddedProviderGateway;
use coomi_provider_adapters::HttpProviderTransport;
use coomi_provider_adapters::ProviderRegistry;
use core_test_support::responses::start_mock_server;
use core_test_support::skip_if_no_network;
use core_test_support::test_codex::test_codex;
use core_test_support::wait_for_event;
use serde_json::Value;
use serde_json::json;
use std::fmt::Write as _;
use std::sync::Arc;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::time::Duration;
use tokio::time::timeout;
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

struct DelayedSequenceResponder {
    next: AtomicUsize,
    responses: Vec<(String, Duration)>,
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

impl Respond for DelayedSequenceResponder {
    fn respond(&self, _request: &Request) -> ResponseTemplate {
        let index = self.next.fetch_add(1, Ordering::SeqCst);
        let (body, delay) = self.responses.get(index).cloned().unwrap_or_else(|| {
            (
                chat_sse(vec![json!({
                    "error": {"message": "unexpected extra model request"}
                })]),
                Duration::ZERO,
            )
        });
        ResponseTemplate::new(200)
            .insert_header("content-type", "text/event-stream")
            .set_delay(delay)
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

#[test]
fn gateway_preserves_turn_and_item_event_identity() -> Result<()> {
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
                "id": "chat-event-identity",
                "choices": [{"delta": {"content": "EVENT_"}, "finish_reason": null}]
            }),
            json!({
                "id": "chat-event-identity",
                "choices": [{"delta": {"content": "IDENTITY_OK"}, "finish_reason": "stop"}]
            }),
        ]);
        mount_chat_response(&provider_server, response, 1).await;

        let gateway = start_mock_gateway(&provider_server).await?;
        let unused_responses_server = start_mock_server().await;
        let test = build_gateway_test(&unused_responses_server, &gateway).await?;
        test.codex
            .submit(user_input_op("Verify canonical event identity."))
            .await?;

        let mut event_index = 0usize;
        let mut turn_started = None;
        let mut item_started = None;
        let mut item_started_index = None;
        let mut first_delta_index = None;
        let mut item_completed = None;
        let mut item_completed_index = None;
        let turn_complete = loop {
            let event = wait_for_event(&test.codex, |_| true).await;
            match event {
                EventMsg::TurnStarted(event) => turn_started = Some(event.turn_id),
                EventMsg::ItemStarted(ItemStartedEvent {
                    thread_id,
                    turn_id,
                    item: TurnItem::AgentMessage(item),
                    ..
                }) => {
                    assert_eq!(thread_id, test.session_configured.thread_id);
                    item_started = Some((turn_id, item.id));
                    item_started_index = Some(event_index);
                }
                EventMsg::AgentMessageContentDelta(event) => {
                    first_delta_index.get_or_insert(event_index);
                    if let Some((turn_id, item_id)) = item_started.as_ref() {
                        assert_eq!(&event.turn_id, turn_id);
                        assert_eq!(&event.item_id, item_id);
                    }
                }
                EventMsg::ItemCompleted(ItemCompletedEvent {
                    thread_id,
                    turn_id,
                    item: TurnItem::AgentMessage(item),
                    ..
                }) => {
                    assert_eq!(thread_id, test.session_configured.thread_id);
                    item_completed = Some((turn_id, item.id));
                    item_completed_index = Some(event_index);
                }
                EventMsg::TurnComplete(event) => break event,
                _ => {}
            }
            event_index += 1;
        };

        let turn_started = turn_started.context("missing TurnStarted")?;
        let item_started = item_started.context("missing agent ItemStarted")?;
        let item_completed = item_completed.context("missing agent ItemCompleted")?;
        assert_eq!(turn_complete.turn_id, turn_started);
        assert_eq!(item_started.0, turn_started);
        assert_eq!(item_completed.0, turn_started);
        assert_eq!(item_completed.1, item_started.1);
        assert_eq!(
            turn_complete.last_agent_message.as_deref(),
            Some("EVENT_IDENTITY_OK")
        );
        assert!(item_started_index < first_delta_index);
        assert!(first_delta_index < item_completed_index);

        gateway.shutdown().await;
        Ok(())
    })
}

#[test]
fn gateway_steer_input_continues_the_active_turn() -> Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_stack_size(TEST_THREAD_STACK_SIZE)
        .enable_all()
        .build()?;

    runtime.block_on(async {
        skip_if_no_network!(Ok(()));

        let provider_server = MockServer::start().await;
        let first = chat_sse(vec![json!({
            "id": "chat-before-steer",
            "choices": [{"delta": {"content": "BEFORE_STEER"}, "finish_reason": "stop"}]
        })]);
        let second = chat_sse(vec![json!({
            "id": "chat-after-steer",
            "choices": [{"delta": {"content": "STEER_GATEWAY_OK"}, "finish_reason": "stop"}]
        })]);
        Mock::given(method("POST"))
            .and(path("/v1/chat/completions"))
            .respond_with(DelayedSequenceResponder {
                next: AtomicUsize::new(0),
                responses: vec![
                    (first, Duration::from_millis(500)),
                    (second, Duration::ZERO),
                ],
            })
            .expect(2)
            .mount(&provider_server)
            .await;

        let gateway = start_mock_gateway(&provider_server).await?;
        let unused_responses_server = start_mock_server().await;
        let test = build_gateway_test(&unused_responses_server, &gateway).await?;
        test.codex.submit(user_input_op("Initial prompt.")).await?;
        let turn_id = match wait_for_event(&test.codex, |event| {
            matches!(event, EventMsg::TurnStarted(_))
        })
        .await
        {
            EventMsg::TurnStarted(event) => event.turn_id,
            _ => unreachable!(),
        };

        test.codex
            .steer_input(
                vec![UserInput::Text {
                    text: "STEER_PROMPT".to_string(),
                    text_elements: Vec::new(),
                }],
                Default::default(),
                Some(&turn_id),
                None,
                None,
            )
            .await
            .map_err(|error| anyhow::anyhow!("failed to steer active turn: {error:?}"))?;
        let completed = wait_for_event(&test.codex, |event| {
            matches!(event, EventMsg::TurnComplete(completed) if completed.turn_id == turn_id)
        })
        .await;
        assert!(matches!(
            completed,
            EventMsg::TurnComplete(event)
                if event.last_agent_message.as_deref() == Some("STEER_GATEWAY_OK")
        ));

        let requests = provider_server
            .received_requests()
            .await
            .context("wiremock request recording is disabled")?;
        assert_eq!(requests.len(), 2);
        assert!(chat_request_has_user_text(&requests[1], "Initial prompt.")?);
        assert!(chat_request_has_user_text(&requests[1], "STEER_PROMPT")?);

        gateway.shutdown().await;
        Ok(())
    })
}

#[test]
fn gateway_interrupt_aborts_a_waiting_provider_turn() -> Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_stack_size(TEST_THREAD_STACK_SIZE)
        .enable_all()
        .build()?;

    runtime.block_on(async {
        tokio::spawn(async {
            skip_if_no_network!(Ok(()));

            let provider_server = MockServer::start().await;
            let delayed = chat_sse(vec![json!({
                "id": "chat-interrupt",
                "choices": [{"delta": {"content": "TOO_LATE"}, "finish_reason": "stop"}]
            })]);
            Mock::given(method("POST"))
                .and(path("/v1/chat/completions"))
                .respond_with(
                    ResponseTemplate::new(200)
                        .set_delay(Duration::from_secs(30))
                        .set_body_raw(delayed, "text/event-stream"),
                )
                .expect(1)
                .mount(&provider_server)
                .await;

            let gateway = start_mock_gateway(&provider_server).await?;
            let unused_responses_server = start_mock_server().await;
            let test = build_gateway_test(&unused_responses_server, &gateway).await?;
            test.codex
                .submit(user_input_op("Wait for interrupt."))
                .await?;
            let turn_id = match wait_for_event(&test.codex, |event| {
                matches!(event, EventMsg::TurnStarted(_))
            })
            .await
            {
                EventMsg::TurnStarted(event) => event.turn_id,
                _ => unreachable!(),
            };
            wait_for_gateway_requests(&gateway, 1).await?;

            test.codex.submit(Op::Interrupt).await?;
            let aborted = wait_for_event(&test.codex, |event| {
                matches!(event, EventMsg::TurnAborted(_))
            })
            .await;
            assert!(matches!(
                aborted,
                EventMsg::TurnAborted(event)
                    if event.turn_id.as_deref() == Some(turn_id.as_str())
                        && event.reason == TurnAbortReason::Interrupted
            ));

            drop(gateway);
            Ok(())
        })
        .await
        .context("gateway interrupt test task panicked")?
    })
}

#[test]
fn gateway_history_survives_rollback_resume_and_fork() -> Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_stack_size(TEST_THREAD_STACK_SIZE)
        .enable_all()
        .build()?;

    runtime.block_on(async {
        tokio::spawn(async {
            skip_if_no_network!(Ok(()));

            let provider_server = MockServer::start().await;
            let responses = [
                ("chat-history-1", "FIRST_REPLY"),
                ("chat-history-2", "SECOND_REPLY"),
                ("chat-history-3", "AFTER_ROLLBACK_REPLY"),
                ("chat-history-4", "AFTER_RESUME_REPLY"),
                ("chat-history-5", "AFTER_FORK_REPLY"),
            ]
            .into_iter()
            .map(|(id, text)| {
                chat_sse(vec![json!({
                    "id": id,
                    "choices": [{"delta": {"content": text}, "finish_reason": "stop"}]
                })])
            })
            .collect();
            Mock::given(method("POST"))
                .and(path("/v1/chat/completions"))
                .respond_with(SequenceResponder {
                    next: AtomicUsize::new(0),
                    responses,
                })
                .expect(5)
                .mount(&provider_server)
                .await;

            let gateway = start_mock_gateway(&provider_server).await?;
            let unused_responses_server = start_mock_server().await;
            let initial = build_gateway_test(&unused_responses_server, &gateway).await?;
            let original_thread_id = initial.session_configured.thread_id;
            initial.submit_turn("FIRST_PROMPT").await?;
            initial.submit_turn("SECOND_PROMPT").await?;

            initial
                .codex
                .submit(Op::ThreadRollback { num_turns: 1 })
                .await?;
            assert!(matches!(
                wait_for_event(&initial.codex, |event| {
                    matches!(event, EventMsg::ThreadRolledBack(_))
                })
                .await,
                EventMsg::ThreadRolledBack(event) if event.num_turns == 1
            ));
            initial.submit_turn("AFTER_ROLLBACK_PROMPT").await?;

            let home = Arc::clone(&initial.home);
            let rollout_path = initial
                .codex
                .rollout_path()
                .context("missing rollout path")?;
            initial.codex.submit(Op::Shutdown).await?;
            wait_for_event(&initial.codex, |event| {
                matches!(event, EventMsg::ShutdownComplete)
            })
            .await;

            let gateway_url = gateway.base_url().to_string();
            let mut resume_builder = test_codex()
                .with_model("deepseek-v4-flash")
                .with_config(move |config| configure_gateway_provider(config, gateway_url));
            let resumed = resume_builder
                .resume(&unused_responses_server, home, rollout_path)
                .await?;
            assert_eq!(resumed.session_configured.thread_id, original_thread_id);
            resumed.submit_turn("AFTER_RESUME_PROMPT").await?;

            let NewThread {
                thread_id: fork_thread_id,
                thread: forked,
                ..
            } = resumed
                .thread_manager
                .fork_thread(
                    ForkSnapshot::Interrupted,
                    resumed.config.clone(),
                    resumed
                        .codex
                        .rollout_path()
                        .context("missing resumed rollout")?,
                    None,
                    None,
                )
                .await?;
            assert_ne!(fork_thread_id, original_thread_id);
            submit_thread_turn(&forked, "AFTER_FORK_PROMPT").await?;

            let requests = provider_server
                .received_requests()
                .await
                .context("wiremock request recording is disabled")?;
            assert_eq!(requests.len(), 5);
            for request in [&requests[2], &requests[3], &requests[4]] {
                assert!(chat_request_has_user_text(request, "FIRST_PROMPT")?);
                assert!(!chat_request_has_user_text(request, "SECOND_PROMPT")?);
                assert!(chat_request_has_user_text(
                    request,
                    "AFTER_ROLLBACK_PROMPT"
                )?);
            }
            assert!(chat_request_has_user_text(
                &requests[3],
                "AFTER_RESUME_PROMPT"
            )?);
            assert!(chat_request_has_user_text(
                &requests[4],
                "AFTER_RESUME_PROMPT"
            )?);
            assert!(chat_request_has_user_text(
                &requests[4],
                "AFTER_FORK_PROMPT"
            )?);

            gateway.shutdown().await;
            Ok(())
        })
        .await
        .context("gateway history test task panicked")?
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

async fn start_mock_gateway(provider_server: &MockServer) -> Result<EmbeddedProviderGateway> {
    let registry = mock_registry(provider_server)?;
    EmbeddedProviderGateway::start(HttpProviderTransport::new(
        registry.active_provider()?.clone(),
    )?)
    .await
    .map_err(Into::into)
}

async fn build_gateway_test(
    responses_server: &MockServer,
    gateway: &EmbeddedProviderGateway,
) -> Result<core_test_support::test_codex::TestCodex> {
    let gateway_url = gateway.base_url().to_string();
    test_codex()
        .with_model("deepseek-v4-flash")
        .with_config(move |config| configure_gateway_provider(config, gateway_url))
        .build(responses_server)
        .await
}

async fn mount_chat_response(provider_server: &MockServer, body: String, expected: u64) {
    Mock::given(method("POST"))
        .and(path("/v1/chat/completions"))
        .respond_with(ResponseTemplate::new(200).set_body_raw(body, "text/event-stream"))
        .expect(expected)
        .mount(provider_server)
        .await;
}

fn user_input_op(text: &str) -> Op {
    Op::UserInput {
        items: vec![UserInput::Text {
            text: text.to_string(),
            text_elements: Vec::new(),
        }],
        final_output_json_schema: None,
        responsesapi_client_metadata: None,
        additional_context: Default::default(),
        thread_settings: Default::default(),
    }
}

async fn submit_thread_turn(thread: &CodexThread, text: &str) -> Result<()> {
    thread.submit(user_input_op(text)).await?;
    let turn_id =
        match wait_for_event(thread, |event| matches!(event, EventMsg::TurnStarted(_))).await {
            EventMsg::TurnStarted(event) => event.turn_id,
            _ => unreachable!(),
        };
    wait_for_event(
        thread,
        |event| matches!(event, EventMsg::TurnComplete(completed) if completed.turn_id == turn_id),
    )
    .await;
    Ok(())
}

fn chat_request_has_user_text(request: &Request, expected: &str) -> Result<bool> {
    let body: Value = serde_json::from_slice(&request.body)?;
    Ok(body["messages"].as_array().is_some_and(|messages| {
        messages.iter().any(|message| {
            message["role"] == "user"
                && message["content"]
                    .as_str()
                    .is_some_and(|content| content.contains(expected))
        })
    }))
}

async fn wait_for_gateway_requests(
    gateway: &EmbeddedProviderGateway,
    expected: usize,
) -> Result<()> {
    timeout(Duration::from_secs(10), async {
        while gateway.request_count() < expected {
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
    })
    .await
    .context("timed out waiting for provider gateway request")?;
    Ok(())
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
