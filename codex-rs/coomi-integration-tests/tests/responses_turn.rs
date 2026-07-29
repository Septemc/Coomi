use anyhow::Result;
use core_test_support::responses::ev_assistant_message;
use core_test_support::responses::ev_completed;
use core_test_support::responses::ev_function_call;
use core_test_support::responses::ev_response_created;
use core_test_support::responses::mount_sse_once;
use core_test_support::responses::sse;
use core_test_support::responses::start_mock_server;
use core_test_support::skip_if_no_network;
use core_test_support::streaming_sse::StreamingSseChunk;
use core_test_support::streaming_sse::start_streaming_sse_server;
use core_test_support::test_codex::test_codex;
use serde_json::Value;
use std::fs;

const TEST_THREAD_STACK_SIZE: usize = 8 * 1024 * 1024;

#[test]
fn codex_core_completes_a_coomi_no_tool_responses_turn() -> Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_stack_size(TEST_THREAD_STACK_SIZE)
        .enable_all()
        .build()?;

    runtime.block_on(async {
        skip_if_no_network!(Ok(()));

        let server = start_mock_server().await;
        let response = mount_sse_once(
            &server,
            sse(vec![
                ev_response_created("coomi-response-1"),
                ev_assistant_message("coomi-message-1", "mock turn complete"),
                ev_completed("coomi-response-1"),
            ]),
        )
        .await;

        let test = test_codex().with_model("gpt-5.2").build(&server).await?;
        let prompt = "Complete this Coomi mock turn without calling a tool.";
        test.submit_turn(prompt).await?;

        let requests = response.requests();
        assert_eq!(
            requests.len(),
            1,
            "a no-tool turn must finish after one model request"
        );

        let request = response.single_request();
        let body = request.body_json();
        assert!(request.body_contains_text(prompt));
        assert_eq!(body.get("stream"), Some(&serde_json::Value::Bool(true)));
        assert!(
            request.inputs_of_type("function_call_output").is_empty(),
            "a no-tool turn must not synthesize tool output"
        );

        Ok(())
    })
}

#[test]
fn c08_stream_retry_executes_a_completed_call_only_once() -> Result<()> {
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .thread_stack_size(TEST_THREAD_STACK_SIZE)
        .enable_all()
        .build()?;

    runtime.block_on(async {
        skip_if_no_network!(Ok(()));

        const CALL_ID: &str = "coomi-c08-call";
        const OUTPUT_FILE: &str = "c08-executions.txt";
        let arguments = serde_json::json!({
            "command": format!("echo coomi-c08 >> {OUTPUT_FILE}"),
        })
        .to_string();

        let incomplete = sse(vec![
            ev_response_created("coomi-c08-incomplete"),
            ev_function_call(CALL_ID, "shell_command", &arguments),
        ]);
        let retried = sse(vec![
            ev_response_created("coomi-c08-retry"),
            ev_function_call(CALL_ID, "shell_command", &arguments),
            ev_completed("coomi-c08-retry"),
        ]);
        let follow_up = sse(vec![
            ev_response_created("coomi-c08-follow-up"),
            ev_assistant_message("coomi-c08-message", "C08 complete"),
            ev_completed("coomi-c08-follow-up"),
        ]);
        let (server, _) = start_streaming_sse_server(vec![
            vec![StreamingSseChunk {
                gate: None,
                body: incomplete,
            }],
            vec![StreamingSseChunk {
                gate: None,
                body: retried,
            }],
            vec![StreamingSseChunk {
                gate: None,
                body: follow_up,
            }],
        ])
        .await;

        let test = test_codex()
            .with_model("gpt-5.4")
            .with_config(|config| {
                config.model_provider.request_max_retries = Some(0);
                config.model_provider.stream_max_retries = Some(1);
                config.model_provider.stream_idle_timeout_ms = Some(2_000);
            })
            .build_with_streaming_server(&server)
            .await?;

        test.submit_turn("Run the C08 retry probe.").await?;

        let requests = server.requests().await;
        assert_eq!(
            requests.len(),
            3,
            "retry and tool follow-up must make three requests"
        );

        let output_counts = requests
            .iter()
            .map(|body| -> Result<usize> {
                let body: Value = serde_json::from_slice(body)?;
                Ok(body
                    .get("input")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter(|item| {
                        item.get("type").and_then(Value::as_str) == Some("function_call_output")
                            && item.get("call_id").and_then(Value::as_str) == Some(CALL_ID)
                    })
                    .count())
            })
            .collect::<Result<Vec<_>>>()?;
        assert_eq!(
            output_counts,
            vec![0, 1, 1],
            "retry and follow-up requests must contain exactly one completed output"
        );

        let output_path = test.workspace_path(OUTPUT_FILE);
        let executions = fs::read(output_path)?
            .iter()
            .filter(|byte| **byte == b'\n')
            .count();
        assert_eq!(executions, 1, "the retried call must execute exactly once");

        server.shutdown().await;
        Ok(())
    })
}
