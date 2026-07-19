#!/usr/bin/env python3
"""Regression tests for the optional Xquik read backend."""

import os
import sys
import unittest
from unittest import mock


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))

import hermes_tweet_client as client


class HermesTweetClientTest(unittest.TestCase):
    def test_normalize_tweet_preserves_xquik_metrics(self):
        tweet = client.normalize_tweet(
            {
                "id": "123",
                "text": "Useful signal",
                "likeCount": 9,
                "retweetCount": 8,
                "replyCount": 7,
                "quoteCount": 6,
                "bookmarkCount": 5,
                "viewCount": 400,
                "author": {"id": "42", "username": "example"},
            },
            "user_tweets",
        )

        self.assertIsNotNone(tweet)
        self.assertEqual(
            tweet["metrics"],
            {
                "like_count": 9,
                "retweet_count": 8,
                "reply_count": 7,
                "quote_count": 6,
                "bookmark_count": 5,
                "impression_count": 400,
            },
        )

    def test_user_tweets_preserves_top_level_cursor_and_page_size(self):
        payload = {
            "tweets": [{"id": "123", "text": "Useful signal"}],
            "next_cursor": "cursor-2",
        }

        with mock.patch.object(client, "_request", return_value=payload) as request:
            tweets, meta = client.fetch_hermes_user_tweets("example", max_results=500)

        self.assertEqual(len(tweets), 1)
        self.assertEqual(meta, {"next_token": "cursor-2"})
        self.assertEqual(request.call_args.args[1]["pageSize"], "100")

    def test_collection_page_sizes_follow_api_bounds(self):
        with mock.patch.object(client, "_request", return_value={}) as request:
            client.fetch_hermes_list_members("1", page_size=1)
            members_params = request.call_args.args[1]
            client.fetch_hermes_following("2", max_results=500)
            following_params = request.call_args.args[1]
            client.fetch_hermes_list_tweets("3", max_results=0)
            tweets_params = request.call_args.args[1]

        self.assertEqual(members_params["pageSize"], "20")
        self.assertEqual(following_params["pageSize"], "200")
        self.assertEqual(tweets_params["pageSize"], "1")


if __name__ == "__main__":
    unittest.main()
