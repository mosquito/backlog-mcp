# Search

Without double quotes, search uses vectors to find related meanings only.
Quoted expressions alone search text only. Add unquoted words to search both:
text search receives only the quoted expressions; vector search receives the whole query
with double quotes removed. Mixed results show text matches first, then vector matches,
without duplicates. Quoted expressions in a mixed query are combined with AND.

## Multiple formulations

`query` requires a list of 1–5 strings, even for one formulation. Use 2–5 short rephrasings of the
same intent, for example in both languages, to improve recall:

```json
{"query": ["recover account access", "восстановить доступ к аккаунту"], "kinds": ["tasks", "memory"]}
```

One formulation uses cosine ranking. Multiple semantic formulations
use equal-weight reciprocal rank fusion (RRF, k=60). Each record appears once;
`limit` applies after fusion. The response echoes the formulations in `queries`.
Semantic `score` is cosine similarity for one formulation and an RRF sum for a
multi-formulation request. `min_score` filters cosine matches before fusion.

Quote rules apply separately to each formulation. Text matches remain first with
`match: "fts"` and no score; they do not enter semantic RRF. Semantic results use
`match: "semantic"`. Exact matches are deduplicated across formulations in input order.

Vector search represents each record by its full text, heading, and body paragraphs.
Short paragraphs remain separate. The record's cosine score is its best passage
score. Results remain records, not separate passages.

## Query syntax

| Query | Finds |
| --- | --- |
| `token` | Related meanings, using vector similarity. |
| `"token" reset "production"` | Text: token AND production; vector: token reset production |
| `"token"` | Only the word token, without prefixes or related meanings. |
| `"token*"` | Only the word token. A star inside quotes does not enable prefixes. |
| `"token"*` | Words starting with token, including tokens and tokenizer. |
| `"refresh token"` | The adjacent words refresh token, in this order. |
| `"refresh token"*` | The word refresh followed by a word starting with token. |
| `"token" "refresh"` | Both words, anywhere in the same field set. |
| `"token" AND "refresh"` | Both words, like the previous example. |
| `"token" OR "password"` | Either word, or both. |
| `"token" NOT "legacy"` | The word token, excluding records with legacy. |
| `("token" OR "password") AND "reset"` | Either token or password, and also reset. |
| `"refresh" + "token"` | The phrase refresh token, like quoted adjacent words. |
| `NEAR("refresh" "token", 5)` | Both words, either order; at most five words between them. |
| `NEAR("refresh" "token")` | The same proximity search with a default distance of ten words. |
| `^"token"` | A field starting with the word token. |
| `title:"token"` | The word token in the title field. |
| `title:("token" OR "password")` | Either word in the title field. |
| `{title body}:"token"` | The word token in title or body. |
| `-tags:"token"` | The word token in fields other than tags. |
| `tags:"web-ui"` | The adjacent words web ui in tags; punctuation is a separator. |
| `"say ""hello"" now"` | The phrase say hello now. Double an inner quote to escape it. |

**Fields:** Tasks and Memory support `title`, `body`, `tags`; Comments support `body`;
Projects support `slug`, `name`, `description`. Select compatible content types before
using a field filter. An unknown field or incomplete expression produces an error.

Words are case-insensitive; punctuation separates words. Quotes match words, not an entire
field or punctuation. Use uppercase `AND`, `OR`, `NOT`, and `NEAR`.
Use parentheses to make grouping explicit.
A trailing star outside quotes is a word-prefix operator; leading and middle wildcards
are not supported. To match punctuation-containing text, put it in double quotes.

**Filters:** Use the Tag filter (`tag` in MCP) for an exact tag, such as `web-ui`.
Choose content types with `kinds`, task statuses with `status`, and scope with `project`,
`all_projects`, `computer`, or `all_computers`. `limit` is 1–200 results per content type.
`min_score` is -1–1 and filters semantic matches only; it never filters text matches.
