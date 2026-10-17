"""
Weights written across several tags, and the default negative.

A weight like `1.5::rain, night ::` is cut into pieces at the commas, and the
app drops pieces in three places: a repeat (split_tags, merge_negative) and a
tag a picture takes out (assemble._apply). Dropping the piece that closes the
weight would let 1.5 run on to the end of the prompt; dropping the piece that
opens it would leave the rest unweighted. The weight stays, the tag goes.

The default negative began as NovelAI's "Heavy" preset, which also pushed away
looks a style asks for by name (film grain, halftone ...). It keeps to quality
now, and a default word the positive asks for stays out of the negative.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from core.services import assemble, library, novelai


class WeightMarkersTests(SimpleTestCase):
    def test_what_stays_of_a_piece(self):
        self.assertEqual(novelai.weight_markers("1.5::rain"), "1.5::")
        self.assertEqual(novelai.weight_markers("-1::hat"), "-1::")
        self.assertEqual(novelai.weight_markers("night ::"), "::")
        self.assertEqual(novelai.weight_markers("1.2::rim light::"), "")
        self.assertEqual(novelai.weight_markers("{night}"), "")

    def test_a_repeat_that_closes_a_weight_leaves_the_close(self):
        self.assertEqual(library.split_tags(["night", "1.5::rain", "night ::", "sky"]),
                         ["night", "1.5::rain", "::", "sky"])

    def test_a_repeat_that_opens_a_weight_leaves_the_open(self):
        self.assertEqual(library.split_tags("night, 1.5::night, rain ::"), ["night", "1.5::", "rain ::"])

    def test_bare_markers_are_never_repeats(self):
        self.assertEqual(library.split_tags("1.5::rain, ::, 1.2::snow, ::"), ["1.5::rain", "::", "1.2::snow", "::"])

    def test_a_tag_taken_out_keeps_the_weight_it_closed(self):
        out = assemble._apply(["1.5::rain", "night ::", "sky"], {"remove_positive": ["night"]}, "positive")
        self.assertEqual(out, ["1.5::rain", "::", "sky"])

    def test_a_self_contained_weight_goes_whole(self):
        out = assemble._apply(["1.1::six heads tall::", "sky"], {"remove_positive": ["six heads tall"]}, "positive")
        self.assertEqual(out, ["sky"])


class DefaultNegativeTests(SimpleTestCase):
    def test_it_does_not_push_away_looks_a_style_asks_for(self):
        default = {novelai.tag_key(t) for t in novelai.DEFAULT_NEGATIVE.split(",")}
        for look in ("film grain", "chromatic aberration", "dithering", "halftone", "screentone", "negative space"):
            self.assertNotIn(look, default)

    def test_a_default_word_the_positive_asks_for_stays_out(self):
        neg = novelai.merge_negative("bad hands", "1girl, multiple views, 1.1::lowres::")
        words = [novelai.tag_key(t) for t in neg.split(",")]
        self.assertNotIn("multiple views", words)
        self.assertNotIn("lowres", words)
        self.assertIn("worst quality", words)
        self.assertEqual(words[0], "bad hands")

    def test_the_picture_own_negative_is_kept_even_if_the_positive_has_it(self):
        """Only the app's own words step aside; what the picture wrote is its call."""
        self.assertIn("film grain", novelai.merge_negative("film grain", "film grain"))
