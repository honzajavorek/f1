from pathlib import Path
import click
import feedparser

import httpx
from lxml import etree
import praw
import prawcore
import stamina


FEED_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:145.0) Gecko/20100101 Firefox/145.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8,cs;q=0.6,sk;q=0.4,es;q=0.2",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-GPC": "1",
    "Priority": "u=0, i",
}


@click.command()
@click.option(
    "--feed",
    "feed_url",
    default="https://old.reddit.com/r/formula1.rss",
    help="RSS feed URL",
)
@click.option(
    "-o",
    "--output",
    "output_path",
    default="feed.xml",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Output file path",
)
@click.option(
    "--client-user-agent", default="F1news (+https://github.com/honzajavorek/f1news/)"
)
@click.option("--client-id", envvar="REDDIT_CLIENT_ID", required=True)
@click.option("--client-secret", envvar="REDDIT_CLIENT_SECRET", required=True)
def main(
    feed_url: str,
    output_path: Path,
    client_user_agent: str,
    client_id: str,
    client_secret: str,
):
    click.echo("Initializing file system")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    click.echo("Initializing Reddit API client")
    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=client_user_agent,
    )
    reddit.read_only = True

    click.echo("Fetching feed")
    response = httpx.get(feed_url, follow_redirects=True, headers=FEED_HTTP_HEADERS)
    response.raise_for_status()

    click.echo("Parsing feed")
    xml_bytes = response.content
    feed = feedparser.parse(xml_bytes)

    url_mapping = {}
    for entry in feed.entries:
        if entry_link := entry.link:
            entry_link = str(entry_link)
            for attempt in stamina.retry_context(on=prawcore.exceptions.Forbidden):
                with attempt:
                    click.echo(f"Fetching: {entry_link} (attempt #{attempt.num})")
                    submission = reddit.submission(url=entry_link)
                    if submission.link_flair_text == ":post-news: News":
                        click.echo(f"Recording as {submission.url}")
                        url_mapping[entry_link] = submission.url
            else:
                click.echo("Not news")
        else:
            click.echo("No link")

    click.echo("Rewriting feed")
    xml = etree.fromstring(xml_bytes)
    namespaces = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in xml.xpath("//atom:entry", namespaces=namespaces):
        link = entry.find("./atom:link", namespaces=namespaces)
        try:
            link.set("href", url_mapping[link.get("href")])
            click.echo(f"Keeping {link.get('href')}")
        except KeyError:
            click.echo(f"Removing {link.get('href')}")
            entry.getparent().remove(entry)

    click.echo(f"Writing feed to {output_path}")
    Path(output_path).write_bytes(
        etree.tostring(xml, pretty_print=True, xml_declaration=True)
    )
