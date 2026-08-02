"""Minimal check for the relevance filter. Run: python test_matcher.py"""
from bot import matches

KW = ["מכבי חיפה", "Maccabi Haifa", "הירוקים"]
EX = ["מכבי תל אביב", 'מכבי ת"א']

assert matches("ניצחון גדול למכבי חיפה בליגה", KW, EX)
assert matches("Maccabi Haifa wins the derby", KW, EX)
assert not matches("מכבי תל אביב חתמה על שחקן", KW, EX)      # excluded
assert not matches("הפועל באר שבע ניצחה", KW, EX)             # no keyword
assert not matches("מכבי חיפה נגד מכבי תל אביב", KW, EX)      # exclusion wins
print("ok")
