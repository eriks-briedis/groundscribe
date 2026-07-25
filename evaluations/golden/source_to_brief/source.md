# Read-through caching for the render pipeline

We shipped a read-through cache in front of the render pipeline in March 2026.
p99 latency fell from 810ms to 120ms on the article pages, measured over the
seven days either side of the change.

## Why the pipeline was slow

Every request re-rendered the whole document tree, including the parts that had
not changed since the last publish. Profiling showed 78% of wall time inside the
Markdown-to-AST step, which is deterministic for a given input.

Rendering being deterministic is what made a cache viable at all: the same input
must produce the same output, or the cache is just a source of stale bugs.

## What went wrong

Invalidation was wrong for the first week. The cache key was built from the
document path alone.

```python
def cache_key(request):
    return request.path
```

Two things fell out of that:

- readers on a non-default locale were served the English render
- an error page rendered during a deploy was cached and served for 40 minutes

We fixed it by adding the locale and the publish revision to the key, and by
refusing to cache any response with a non-200 status.

## What I would do differently

I think the real mistake was treating invalidation as a follow-up task rather
than part of the cache design. My guess is that most teams shipping a cache under
deadline make the same trade, though I have no data on that.

> The internal postmortem covering the deploy itself is not publishable.
