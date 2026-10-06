# 4. Embeddings

> **Status:** Not checked: that semantic search finds a post by a different word with the same meaning (4.5).

Gives every topic a vector, which the ranking service (`server/README.md`) compares against each researcher's interest profile, and turns on semantic search and Related Topics for the people. The `discourse-ai` plugin, which ships in Discourse core, embeds; a small local model on CPU serves the embeddings.

Do not clone `discourse-ai` in `app.yml`: it is already in `plugins/`, and a clone fails the bootstrap with "destination path 'discourse-ai' already exists".

## 4.1 The embedding server

```bash
sudo DISCOURSE_DATA_DIR=/mnt/qupa-cafe docker compose -f server/tei-compose.yml up -d
```

Text Embeddings Inference, `cpu-1.8`, serving `BAAI/bge-small-en-v1.5` (384 dimensions, 512 tokens), with its model cache in `$DISCOURSE_DATA_DIR/tei-cache`. Three details in the compose file matter:

- It binds the **Docker bridge gateway**, `172.17.0.1:8081`. On `127.0.0.1` the Discourse container cannot reach it (its loopback is its own); on `0.0.0.0` it is public, since Docker bypasses `ufw`. Find the gateway with `docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'`.
- The image must be a `cpu-*` tag; the default tag looks for a GPU.
- It is capped at 2 CPUs and 2 GB so a burst cannot starve Unicorn.

Check it from the host and from inside the container; the second is the one that matters:

```bash
curl -s -X POST http://172.17.0.1:8081/embed -H 'Content-Type: application/json' -d '{"inputs":"probe"}' | head -c 80
sudo docker exec app curl -s -o /dev/null -w '%{http_code}\n' -X POST http://172.17.0.1:8081/embed -H 'Content-Type: application/json' -d '{"inputs":"probe"}'
```

An array of floats, then `200`.

## 4.2 Configure the plugin

There is no preset for a 384-dimension model, and a definition will not save without a credential even for a server with no authentication. In the container's console (`sudo /var/discourse/launcher enter app`, then `rails c`):

```ruby
secret = AiSecret.create!(name: "local-tei", secret: "unused-local-tei-needs-no-auth", created_by_id: 1)
definition = EmbeddingDefinition.create!(
  display_name:        "bge-small-en-v1.5 (local TEI)",
  provider:            "hugging_face",
  url:                 "http://172.17.0.1:8081/embed",
  dimensions:          384,
  max_sequence_length: 512,
  pg_function:         "<#>",
  tokenizer_class:     "DiscourseAi::Tokenizer::BgeLargeEnTokenizer",
  ai_secret_id:        secret.id,
)
SiteSetting.allowed_internal_hosts = "172.17.0.1"
SiteSetting.ai_embeddings_selected_model = definition.id
SiteSetting.ai_embeddings_enabled = true
SiteSetting.ai_embeddings_semantic_search_enabled = true
SiteSetting.ai_embeddings_semantic_related_topics_enabled = true
Jobs::EmbeddingsBackfill.new.execute({})
```

- `allowed_internal_hosts` is the exception to Discourse's SSRF guard, which blocks private addresses, the bridge gateway included. The provisioning webhook (5.3) needs it too.
- `ai_embeddings_selected_model` takes the definition's id. `pg_function` `<#>` matches the BGE presets; the BGE family shares the `BgeLargeEn` tokenizer.

## 4.3 Latency

Only the scheduled `Jobs::EmbeddingsBackfill`, every 5 minutes, was seen embedding: a new topic was embedded after about two minutes and the event callback did nothing observable. Enabling the settings embeds nothing until that job runs, hence the backfill run above. The ranking service treats a missing vector as "not yet" and does not advance past it, so a slow embedding delays a wake rather than losing it.

## 4.4 Topics are embedded, posts are not

Vectors are in `ai_topics_embeddings(topic_id, model_id, strategy_id, ..., embeddings)`, keyed on all three, so a forum with a second definition holds several rows per topic (`EMBEDDINGS_MODEL_ID` in `ranking.py` filters). `ai_posts_embeddings` exists and stays empty; the hidden setting `ai_embeddings_per_post_enabled` would fill it and is untested here. So `server/ranking.py` scores topics and applies its guardrails per post. `pgvector` is already installed.

## 4.5 Verification

```bash
sudo docker exec app rails runner 'puts DB.query_single("select count(*) from ai_topics_embeddings").first'
```

The count should equal the number of topics (deleted topics leave rows behind). Zero means the backfill has not run yet. Then post something and search for it by a different word with the same meaning.
