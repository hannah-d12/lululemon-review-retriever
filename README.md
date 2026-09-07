# Lululemon Review Retriever

Small API used by the Cling Review Analyzer. It accepts one or more public lululemon PDP URLs and returns the accessible review corpus for each product.

## Why this exists

A normal ChatGPT Skill can analyze reviews but may not be allowed to make the dynamic lululemon GraphQL request. This service performs that retrieval in a real Chromium browser, captures the page's own `GetReviews` request, then paginates it.

## Endpoint

`POST /v1/lululemon/reviews`

Request:

```json
{
  "urls": [
    "https://shop.lululemon.com/en-ca/p/mens-ls-tops/Metal-Vent-Tech-Long-Sleeve-Shirt-3/_/prod11710150?color=69299"
  ]
}
```

Response shape:

```json
{
  "products": [
    {
      "url": "...",
      "product_name_id": "Metal_Vent_Tech_Long_Sleeve_Shirt_3_Upd",
      "total_results": 278,
      "reviews_retrieved": 278,
      "coverage": 1.0,
      "reviews": [ ... ],
      "error": null
    }
  ]
}
```

## Local run with Docker

```bash
docker build -t lulu-review-retriever .
docker run --rm -p 8000:8000 lulu-review-retriever
```

Then test:

```bash
curl -X POST http://localhost:8000/v1/lululemon/reviews \
  -H 'content-type: application/json' \
  -d '{"urls":["https://shop.lululemon.com/en-ca/p/mens-ls-tops/Metal-Vent-Tech-Long-Sleeve-Shirt-3/_/prod11710150?color=69299"]}'
```

## Deploy

Deploy this Docker container to any service that supports long-running HTTP containers, such as Render, Railway, Fly.io, or your organization's cloud. Use a region where the retailer site is accessible.

After deployment, replace `https://YOUR-DEPLOYED-DOMAIN` in `openapi.yaml` with the service URL. That OpenAPI document can then be used to connect the retriever to a custom ChatGPT integration/plugin.

## Retrieval behavior

1. Open PDP in headless Chromium.
2. Wait for / trigger the review widget.
3. Capture the page's `GetReviews` GraphQL request, including `productNameId`.
4. Remove any search/rating/filter restriction.
5. Request offsets sequentially until `hasAdditionalReviews` is false.
6. Deduplicate reviews by review ID.
7. Return `total_results`, `reviews_retrieved`, and `coverage`.

The service intentionally processes URLs sequentially to reduce load and the likelihood of anti-bot throttling.

## Research safeguard

Downstream analysis should only calculate prevalence against a product's full stated review count when `reviews_retrieved == total_results` (or coverage is otherwise explicitly accepted by the researcher).
