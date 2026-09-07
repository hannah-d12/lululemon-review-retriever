import asyncio
import json
import os
import re
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl
from playwright.async_api import async_playwright, Request

app = FastAPI(title="Lululemon Review Retriever", version="0.1.0")


class ReviewRequest(BaseModel):
    urls: List[HttpUrl]


class ProductResult(BaseModel):
    url: str
    product_name_id: Optional[str] = None
    total_results: int = 0
    reviews_retrieved: int = 0
    coverage: float = 0.0
    reviews: List[Dict[str, Any]] = []
    error: Optional[str] = None


class ReviewResponse(BaseModel):
    products: List[ProductResult]


GET_REVIEWS_OPERATION = "GetReviews"


def _clean_headers(headers: Dict[str, str]) -> Dict[str, str]:
    keep = {
        "accept",
        "content-type",
        "x-lll-client",
        "x-lll-client-repo-name",
        "x-lll-locale",
        "x-lll-referrer",
    }
    out = {k.lower(): v for k, v in headers.items() if k.lower() in keep}
    out.setdefault("accept", "application/json")
    out.setdefault("content-type", "application/json")
    out.setdefault("x-lll-client", "pdp-reviews-component")
    out.setdefault("x-lll-client-repo-name", "product-experiences")
    out.setdefault("x-lll-locale", "en-US")
    out.setdefault("x-lll-referrer", "Channel=Web,Page=pdp")
    return out


def _parse_getreviews_request(req: Request) -> Optional[Dict[str, Any]]:
    if "/cne/graphql" not in req.url or req.method.upper() != "POST":
        return None
    try:
        body = req.post_data_json
    except Exception:
        body = None
    if not isinstance(body, dict):
        return None
    if body.get("operationName") != GET_REVIEWS_OPERATION:
        return None
    variables = body.get("variables") or {}
    product_name_id = variables.get("productNameId")
    if not product_name_id:
        return None
    return {
        "endpoint": req.url,
        "query": body.get("query"),
        "variables": variables,
        "operationName": body.get("operationName"),
        "headers": _clean_headers(req.headers),
        "productNameId": product_name_id,
    }


async def _capture_getreviews_template(page, timeout_ms: int = 30000) -> Dict[str, Any]:
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def on_request(req: Request):
        if future.done():
            return
        parsed = _parse_getreviews_request(req)
        if parsed:
            future.set_result(parsed)

    page.on("request", on_request)

    # First try natural page load / review widget initialization.
    try:
        await page.wait_for_timeout(2500)
        if future.done():
            return future.result()

        # Scroll progressively to trigger lazy review loading.
        for frac in (0.5, 0.75, 0.9, 1.0):
            await page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {frac})")
            await page.wait_for_timeout(1200)
            if future.done():
                return future.result()

        # Try to interact with common review controls if present.
        selectors = [
            'input[placeholder*="Search Reviews" i]',
            'input[aria-label*="Search Reviews" i]',
            'button:has-text("Newest")',
            'button:has-text("Most Relevant")',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    if "input" in sel:
                        await loc.fill("metal")
                        await loc.press("Enter")
                    else:
                        await loc.click()
                    await page.wait_for_timeout(1500)
                    if future.done():
                        return future.result()
            except Exception:
                pass

        return await asyncio.wait_for(future, timeout=timeout_ms / 1000)
    finally:
        page.remove_listener("request", on_request)


async def _fetch_all_reviews(page, template: Dict[str, Any]) -> Dict[str, Any]:
    endpoint = template["endpoint"]
    headers = template["headers"]
    query = template["query"]
    base_variables = dict(template["variables"])

    # Always retrieve the full corpus, not a prior search-filtered subset.
    base_variables["search"] = None
    base_variables["rating"] = []
    base_variables["filters"] = []
    base_variables.setdefault("sort", "SubmissionTime:desc")

    all_reviews: List[Dict[str, Any]] = []
    seen_ids = set()
    offset = 0
    total_results: Optional[int] = None
    limit: Optional[int] = None

    while True:
        variables = dict(base_variables)
        variables["offset"] = offset
        payload = {
            "query": query,
            "variables": variables,
            "operationName": GET_REVIEWS_OPERATION,
        }

        response = await page.request.post(endpoint, headers=headers, data=json.dumps(payload))
        if not response.ok:
            raise RuntimeError(f"GraphQL request failed with HTTP {response.status}")
        data = await response.json()
        reviews_data = ((data or {}).get("data") or {}).get("getReviews")
        if not reviews_data:
            raise RuntimeError(f"Unexpected GraphQL response: {str(data)[:500]}")
        if reviews_data.get("hasErrors"):
            raise RuntimeError(f"Review API errors: {reviews_data.get('errors')}")

        total_results = int(reviews_data.get("totalResults") or 0)
        limit = int(reviews_data.get("limit") or 0)
        batch = reviews_data.get("results") or []

        for review in batch:
            rid = str(review.get("id"))
            if rid and rid not in seen_ids:
                seen_ids.add(rid)
                all_reviews.append(review)

        if not reviews_data.get("hasAdditionalReviews"):
            break
        if limit <= 0:
            raise RuntimeError("Pagination reported additional reviews but no positive limit")
        offset += limit
        if total_results and offset >= total_results:
            break

    return {
        "product_name_id": template["productNameId"],
        "total_results": total_results or len(all_reviews),
        "reviews": all_reviews,
    }


async def retrieve_one(url: str) -> ProductResult:
    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-http2",
        "--disable-quic",
]
async with async_playwright() as p:
    browser = await p.chromium.launch(headless=True, args=browser_args)
    context = await browser.new_context(
        locale="en-CA",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="commit", timeout=60000)
        await page.wait_for_timeout(3000)
        template = await _capture_getreviews_template(page)
        fetched = await _fetch_all_reviews(page, template)
        total = int(fetched["total_results"])
        reviews = fetched["reviews"]
        retrieved = len(reviews)
        coverage = (retrieved / total) if total else 0.0
        return ProductResult(
            url=url,
            product_name_id=fetched["product_name_id"],
            total_results=total,
            reviews_retrieved=retrieved,
            coverage=coverage,
            reviews=reviews,
        )
    except Exception as e:
        return ProductResult(url=url, error=str(e))
    finally:
        await context.close()
        await browser.close()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/lululemon/reviews", response_model=ReviewResponse)
async def get_reviews(body: ReviewRequest):
    if not body.urls:
        raise HTTPException(status_code=400, detail="At least one URL is required")
    if len(body.urls) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 URLs per request")

    urls = [str(u) for u in body.urls]
    results = []
    # Sequential on purpose: kinder to retailer and less likely to trigger anti-bot controls.
    for url in urls:
        if "lululemon.com" not in url.lower():
            results.append(ProductResult(url=url, error="Only lululemon.com URLs are supported in v0.1"))
            continue
        results.append(await retrieve_one(url))
    return ReviewResponse(products=results)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
