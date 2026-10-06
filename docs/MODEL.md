# Model runtime acceptance

Default: Qwen3-4B-Instruct-2507, Unsloth's Q4_K_M GGUF at the revision, size and SHA-256 in
`deploy/models.lock`. Runtime: CPU llama.cpp b11434 at the digest/commit in `docker/model/VERSION`.

The Contabo server has not been accessed or benchmarked by this change. There are no
measured production speed or memory numbers yet. Earlier spec measurements used another
VM and another llama.cpp build; they are not acceptance results for this runtime.

Before final deployment, record:

| Check | Required result | Actual-host result |
|---|---|---|
| Default GGUF installation | Catalogue size and SHA-256 match | Pending |
| Model health and chat | Local authenticated reply succeeds | Pending |
| Peak model memory at 6144 context | At most 3600 MiB; otherwise reduce MODEL_CTX to 5120 and remeasure | Pending |
| Prompt and generation speeds | Record tokens/s at short and 4096-token contexts | Pending |
| Network isolation | No model egress or published port | Pending |

CI uses a 1.2 MB TinyStories fixture to check runtime flags, authenticated JSON-schema
inference, disabled slots, read-only weights, egress refusal and restart health. It proves
container/protocol behavior, not Qwen quality, production throughput or memory. The
provider/grammar/context integration and `make model-bench` arrive in later M2 work.
