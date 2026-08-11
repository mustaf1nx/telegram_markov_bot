import random
import unittest

from markov import MarkovChain


class MarkovChainTests(unittest.TestCase):
    def test_empty_corpus_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MarkovChain(["", "# comment"])

    def test_generated_text_uses_corpus_words(self) -> None:
        chain = MarkovChain(
            ["Добро пожаловать в наше сообщество!"],
            rng=random.Random(7),
        )
        self.assertEqual(chain.generate(), "Добро пожаловать в наше сообщество!")

    def test_generation_can_mix_sentences(self) -> None:
        chain = MarkovChain(
            [
                "Добро пожаловать в наш дружный дом.",
                "Добро пожаловать в наш интересный чат.",
                "Заходи скорее в наш дружный чат.",
            ],
            order=1,
            rng=random.Random(3),
        )
        result = chain.generate(attempts=50)
        self.assertTrue(result)
        self.assertLessEqual(len(chain.tokenize(result)), 35)

    def test_comments_are_ignored(self) -> None:
        chain = MarkovChain(["# пояснение", "Привет, друг!"])
        self.assertEqual(chain.generate(), "Привет, друг!")


if __name__ == "__main__":
    unittest.main()
