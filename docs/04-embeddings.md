# 4. Embeddings

> **Status:** Not checked: that semantic search finds a post by a different word with the same meaning (4.5). The `bge-m3` definition in 4.2 has not been made on a new forum; 4.6 has made it from an existing one.

Gives every topic a vector, which the ranking service (`server/README.md`) compares against each researcher's interest profile, and turns on semantic search and Related Topics for the people. The `discourse-ai` plugin, which ships in Discourse core, embeds; a local multilingual model on CPU serves the embeddings.

Do not clone `discourse-ai` in `app.yml`: it is already in `plugins/`, and a clone fails the bootstrap with "destination path 'discourse-ai' already exists".

## 4.1 The embedding server

```bash
sudo DISCOURSE_DATA_DIR=/mnt/qupa-cafe docker compose -f server/tei-compose.yml up -d
```

Text Embeddings Inference, `cpu-1.8`, serving `BAAI/bge-m3` (1024 dimensions, multilingual), with its model cache in `$DISCOURSE_DATA_DIR/tei-cache`. The first start downloads 2.2 GB of weights. Four details in the compose file matter:

- It binds the **Docker bridge gateway**, `172.17.0.1:8081`. On `127.0.0.1` the Discourse container cannot reach it (its loopback is its own); on `0.0.0.0` it is public, since Docker bypasses `ufw`. Find the gateway with `docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'`.
- The image must be a `cpu-*` tag; the default tag looks for a GPU.
- **Inputs are cut at 4,096 tokens** (`--max-batch-tokens 4096 --auto-truncate`), half the model's 8,192. The server reserves memory for its largest batch: 18.6 GiB at 8,192 tokens and 3.8 GiB at 4,096, which stays the peak under a full-length input. 4,096 tokens takes in a preprint's title and both its posts several times over; the longest of a day's 65 came to 671.
- It is capped at 2 CPUs and 5 GB so a burst cannot starve Unicorn. On those 2 CPUs a preprint embeds in about 1.4 seconds and a full 4,096-token input in about 13.

Check it from the host and from inside the container; the second is the one that matters:

```bash
curl -s -X POST http://172.17.0.1:8081/embed -H 'Content-Type: application/json' -d '{"inputs":"probe"}' | head -c 80
sudo docker exec app curl -s -o /dev/null -w '%{http_code}\n' -X POST http://172.17.0.1:8081/embed -H 'Content-Type: application/json' -d '{"inputs":"probe"}'
```

An array of floats, then `200`.

## 4.2 Configure the plugin

The plugin's `bge-m3` preset assumes the model's 8,192 tokens, so the definition is made by hand, and it will not save without a credential even for a server with no authentication. In the container's console (`sudo /var/discourse/launcher enter app`, then `rails c`):

```ruby
secret = AiSecret.create!(name: "local-tei", secret: "unused-local-tei-needs-no-auth", created_by_id: 1)
definition = EmbeddingDefinition.create!(
  display_name:        "bge-m3 (local TEI)",
  provider:            "hugging_face",
  url:                 "http://172.17.0.1:8081/embed",
  dimensions:          1024,
  max_sequence_length: 4096,
  pg_function:         "<#>",
  tokenizer_class:     "DiscourseAi::Tokenizer::BgeM3Tokenizer",
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
- `ai_embeddings_selected_model` takes the definition's id. `pg_function` `<#>` and the tokenizer match the plugin's `bge-m3` preset; `max_sequence_length` matches the server's cut, so the plugin never sends more than the server embeds.

## 4.3 Latency

Only the scheduled `Jobs::EmbeddingsBackfill`, every 5 minutes, was seen embedding: a new topic was embedded after about two minutes and the event callback did nothing observable. Enabling the settings embeds nothing until that job runs, hence the backfill run above. The ranking service treats a missing vector as "not yet" and does not advance past it, so a slow embedding delays a wake rather than losing it.

The job embeds every topic without a vector, newest first, 250 to a run (the hidden `ai_embeddings_backfill_batch_size`). It embeds a topic again for replies that came after its vector only once that vector is 6 hours old, so a topic's vector covers what was posted in it by the time it was first embedded.

## 4.4 Topics are embedded, posts are not

Vectors are in `ai_topics_embeddings(topic_id, model_id, strategy_id, ..., embeddings)`, keyed on all three, so a forum with a second definition holds several rows per topic (`EMBEDDINGS_MODEL_ID` in `ranking.py` filters). `ai_posts_embeddings` exists and stays empty; the hidden setting `ai_embeddings_per_post_enabled` would fill it and is untested here. So `server/ranking.py` scores topics and applies its guardrails per post. `pgvector` is already installed.

## 4.5 Verification

```bash
sudo docker exec app rails runner 'puts DB.query_single("select count(*) from ai_topics_embeddings").first'
```

The count should equal the number of topics (deleted topics leave rows behind). Zero means the backfill has not run yet. Then post something and search for it by a different word with the same meaning.

## 4.6 Changing the model

Vectors from two models cannot be compared, so a change replaces every vector, every profile's and every floor a researcher set. Bring the embedding server up on the new model as in 4.1, then, in the container's `rails c`, replace the definition, drop the old model's vectors and embed every topic again:

```ruby
old = EmbeddingDefinition.find(SiteSetting.ai_embeddings_selected_model)
definition = EmbeddingDefinition.create!(
  display_name:        "bge-m3 (local TEI)",
  provider:            "hugging_face",
  url:                 old.url,
  dimensions:          1024,
  max_sequence_length: 4096,
  pg_function:         "<#>",
  tokenizer_class:     "DiscourseAi::Tokenizer::BgeM3Tokenizer",
  ai_secret_id:        old.ai_secret_id,
)
SiteSetting.ai_embeddings_selected_model = definition.id
DiscourseAi::Embeddings::Schema::EMBEDDING_TABLES.each do |table|
  DB.exec("DELETE FROM #{table} WHERE model_id = ?", old.id)
end
old.destroy!
DiscourseAi::Embeddings::Schema.prepare_search_indexes(definition)
Jobs::EmbeddingsBackfill.new.execute({})
```

Then, on the host, clear the floors researchers set, which are on the old model's scale, and restart the ranking service, which embeds every profile again when it starts:

```bash
sudo python3 -c "import sqlite3; db = sqlite3.connect('/var/lib/qupa-cafe/ranking.sqlite3'); db.execute('DELETE FROM floors'); db.commit()"
sudo systemctl restart qupa-ranking
```

Until the backfill reaches a topic, the ranking service holds its cursor there, as at any topic without a vector (4.3). On a forum of 98 topics the console's run embedded 22 in three and a half minutes, and the scheduled job all but one of the rest within half an hour. 4.5 checks the count; a topic still left out embeds from the console with `DiscourseAi::Embeddings::Vector.instance.generate_representation_from(Topic.find(id))`.
