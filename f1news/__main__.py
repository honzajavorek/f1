import asyncio
import logging
from pathlib import Path
from urllib.parse import parse_qs
import click
from crawlee import ConcurrencySettings
import feedparser

from crawlee.beautifulsoup_crawler import (
    BeautifulSoupCrawler,
    BeautifulSoupCrawlingContext,
)
from crawlee.configuration import Configuration
from crawlee.router import Router
from crawlee._log_config import CrawleeLogFormatter
import httpx
from lxml import etree
from apify import Actor


handler = logging.StreamHandler()
handler.setFormatter(CrawleeLogFormatter())
logger = logging.getLogger("f1news")
logger.setLevel(logging.INFO)
logger.addHandler(handler)


router = Router[BeautifulSoupCrawlingContext]()


@click.command()
@click.option(
    "--feed",
    "feed_url",
    default="https://www.reddit.com/r/formula1.rss",
    help="RSS feed URL",
)
@click.option(
    "--output",
    "output_path",
    default="feed.xml",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Output file path",
)
@click.option("--debug/--no-debug", default=False, help="Enable debug mode")
def main(feed_url: str, output_path: Path, debug: bool):
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    rss, url_mapping = asyncio.run(scrape(feed_url, debug=debug))
    xml = etree.fromstring(rss)
    namespaces = {'atom': 'http://www.w3.org/2005/Atom'}
    for entry in xml.xpath("//atom:entry", namespaces=namespaces):
        link = entry.find("./atom:link", namespaces=namespaces)
        try:
            link.set("href", url_mapping[link.get("href")])
            logger.info(f"Keeping {link.get('href')}")
        except KeyError:
            logger.info(f"Removing {link.get('href')}")
            entry.getparent().remove(entry)
    Path(output_path).write_bytes(
        etree.tostring(xml, pretty_print=True, xml_declaration=True)
    )


async def scrape(feed_url: str, debug: bool = False):
    async with httpx.AsyncClient() as client:
        response = await client.get(feed_url)
        response.raise_for_status()
        rss = response.content

    feed = feedparser.parse(rss)
    links = [entry.link for entry in feed.entries]

    async with Actor:
        try:
            proxy_configuration = await Actor.create_proxy_configuration()
        except ValueError:
            logger.warning("Couldn't infer proxy configuration")
            proxy_configuration = None

        max_concurrency = 1 if Actor.is_at_home() else 5
        logger.info(f"Max concurrency: {max_concurrency}")

        max_request_retries = 50 if Actor.is_at_home() else 3
        logger.info(f"Max request retries: {max_request_retries}")

        crawler = BeautifulSoupCrawler(
            request_handler=router,
            proxy_configuration=proxy_configuration,
            concurrency_settings=ConcurrencySettings(max_concurrency=max_concurrency),
            max_request_retries=max_request_retries,
            additional_http_error_status_codes=[403, 429],
            configuration=Configuration(log_level="DEBUG" if debug else "INFO"),
        )
        await crawler.run(links)
        dataset = await crawler.get_dataset()

    url_mapping = {}
    async for article in dataset.iterate_items():
        url_mapping[article["reddit_url"]] = article["article_url"]

    return rss, url_mapping


@router.default_handler
async def default_handler(context: BeautifulSoupCrawlingContext):
    context.log.info(f"Scraping {context.request.url} (proxy: {context.proxy_info})")

    flair_link = context.soup.select_one('a[href*="/r/formula1/?f=flair_name"]')
    _, query_string = flair_link["href"].split("?")
    f_param_value = parse_qs(query_string)["f"][0]
    if f_param_value != 'flair_name:":post-news: News"':
        context.log.info(f"Not news: {context.request.url}")
        return

    article_link = context.soup.select_one(
        'a[aria-label][target="_blank"][rel*="nofollow"][rel*="noopener"]'
    )
    article_url = article_link["href"]

    data = {"reddit_url": context.request.url, "article_url": article_url}
    context.log.info(f"Saving {data!r}")
    await context.push_data(data)
