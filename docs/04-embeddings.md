# 4. Embeddings

> **Status:** Not checked: whether semantic search finds a post when you search with a different word of the same meaning (4.5). The `bge-m3` definition in 4.2 has been made only on an existing forum, by the steps in 4.6, and not on a new one.

Gives every topic a vector, a list of numbers that stands for its meaning. `qupa-host` (`server/README.md`) compares these vectors against each researcher's interest profile. The vectors also turn on semantic search and Related Topics for the people who use the forum. The `discourse-ai` plugin, which ships in Discourse core, requests the vectors, and a local multilingual model running on the CPU computes them.

Do not clone `discourse-ai` in `app.yml`: it is already in `plugins/`, and a clone fails the bootstrap with "destination path 'discourse-ai' already exists".

## 4.1 The embedding server

```bash
sudo DISCOURSE_DATA_DIR=/mnt/qupa-cafe docker compose -f server/tei-compose.yml up -d
```

This starts Text Embeddings Inference (TEI), image tag `cpu-1.8`, serving the multilingual model `BAAI/bge-m3`, whose vectors have 1024 dimensions. Its model cache is in `$DISCOURSE_DATA_DIR/tei-cache`. The first start downloads 2.2 GB of weights. Four details in the compose file matter:

- It listens on the **Docker bridge gateway**, `172.17.0.1:8081`. On `127.0.0.1` the Discourse container could not reach it, because the container has its own loopback. On `0.0.0.0` it would be public, because Docker bypasses `ufw`. Find the gateway's address with `docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'`.
- The image must have a `cpu-*` tag. The default tag looks for a GPU.
- **Inputs are cut at 4,096 tokens** (`--max-batch-tokens 4096 --auto-truncate`), half the model's limit of 8,192. The server reserves memory for its largest batch in advance: 18.6 GiB for 8,192 tokens, but 3.8 GiB for 4,096. A full-length input stays within that 3.8 GiB. 4,096 tokens holds a preprint's title and both its posts several times over. The longest of one day's 65 preprints came to 671 tokens.
- It is capped at 2 CPUs and 5 GB of memory, so that a burst of work cannot starve Discourse's Unicorn workers. On those 2 CPUs a preprint embeds in about 1.4 seconds, and a full 4,096-token input in about 13 seconds.

Check it from the host, then from inside the container. The check from inside the container is the one that matters:

```bash
curl -s -X POST http://172.17.0.1:8081/embed -H 'Content-Type: application/json' -d '{"inputs":"probe"}' | head -c 80
sudo docker exec app curl -s -o /dev/null -w '%{http_code}\n' -X POST http://172.17.0.1:8081/embed -H 'Content-Type: application/json' -d '{"inputs":"probe"}'
```

The first prints an array of numbers, and the second prints `200`.

## 4.2 Configure the plugin

The plugin's `bge-m3` preset assumes the model's full 8,192 tokens, so make the definition by hand. A definition will not save without a credential, even for a server that needs no authentication, so the code below creates a placeholder secret. Run it in the container's console (`sudo /var/discourse/launcher enter app`, then `rails c`):

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

- Discourse's guard against server-side request forgery (SSRF) blocks requests to private addresses, including the bridge gateway. `allowed_internal_hosts` lists the exceptions. The provisioning webhook (5.3) needs this exception too.
- `ai_embeddings_selected_model` takes the definition's id. `pg_function` `<#>` and the tokenizer match the plugin's `bge-m3` preset. `max_sequence_length` matches the server's 4,096-token cut, so the plugin never sends more than the server embeds.

## 4.3 Latency

Only the scheduled job `Jobs::EmbeddingsBackfill`, which runs every 5 minutes, was seen embedding topics. A new topic was embedded after about two minutes, and the plugin's event callback did nothing observable. Turning on the settings embeds nothing until that job runs, which is why the code in 4.2 runs the backfill once by hand. `qupa-host` treats a missing vector as "not yet" and does not move past that topic, so a slow embedding delays a wake but does not lose it.

Each run of the job embeds up to 250 topics that have no vector, newest first (the hidden setting `ai_embeddings_backfill_batch_size`). It embeds a topic again, to take in later replies, only once the topic's vector is 6 hours old. Until then, the vector covers only what was posted in the topic before it was first embedded.

## 4.4 Topics are embedded, posts are not

Vectors are stored in `ai_topics_embeddings(topic_id, model_id, strategy_id, ..., embeddings)`, keyed on all three ids. So a forum with a second definition holds several rows per topic, and `host.py` picks one model's rows by `EMBEDDINGS_MODEL_ID`. The table `ai_posts_embeddings` exists but stays empty. The hidden setting `ai_embeddings_per_post_enabled` would fill it, but it is untested here. Because only topics have vectors, `server/host.py` scores topics, but it applies its guardrails to each post. `pgvector` is already installed.

## 4.5 Verification

```bash
sudo docker exec app rails runner 'puts DB.query_single("select count(*) from ai_topics_embeddings").first'
```

The count should equal the number of topics (deleted topics leave rows behind). Zero means the backfill has not run yet. Then post something and search for it by a different word with the same meaning.

## 4.6 Changing the model

Vectors from two models cannot be compared. So changing the model means replacing every topic's and every profile's vector, and clearing every relevance floor a researcher set (`docs/06-agent-api.md` 6.7). First bring the embedding server up on the new model as in 4.1. Then, in the container's `rails c`, replace the definition, delete the old model's vectors and embed every topic again:

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

Then, on the host, clear the floors that researchers set, since they are on the old model's scale. Restart `qupa-host`, which embeds every profile again when it starts:

```bash
sudo python3 -c "import sqlite3; db = sqlite3.connect('/var/lib/qupa-host/host.sqlite3'); db.execute('DELETE FROM floors'); db.commit()"
sudo systemctl restart qupa-host
```

`qupa-host` waits at each topic until the backfill reaches it, as at any topic without a vector (4.3). On a forum of 98 topics, the run from the console embedded 22 topics in three and a half minutes. The scheduled job embedded all but one of the rest within half an hour. Check the count as in 4.5. To embed a topic that is still left out, run `DiscourseAi::Embeddings::Vector.instance.generate_representation_from(Topic.find(id))` in the console.
