from string import ascii_letters

# Set membership is much faster than calling .lower() and comparing for every
# character, and this check runs once per character scanned.
_LETTERS = frozenset(ascii_letters)


def _is_letter(c: str) -> bool:
    # True only for ASCII a-z / A-Z. Digits, punctuation, spaces and
    # accented letters all count as "not a letter" (i.e. separators).
    return c in _LETTERS


def prev_words(text: str, pos: int, n: int = 3, join: str = "'’", stop_at: str = "") -> list[str]:
    """
    Return up to n words that come before position `pos` in `text`,
    scanning backward as far as needed.

    text     -- the full text being searched
    pos      -- where the regex match starts (e.g. m.start())
    n        -- how many words to collect
    join     -- characters allowed *inside* a word (apostrophes by
                default, so "don't" -> "dont"); add "-" to join hyphens
    stop_at  -- characters that end the search early, e.g. ".!?" to stay
                within the current sentence; "" means never stop early

    Words are returned in reading order, so the nearest word is last.
    """
    words = []   # collected backward: nearest word first, reversed at the end
    i = pos      # cursor; text[i - 1] is always the next character to inspect

    # If the match starts in the middle of a word (e.g. "acctINV-1"),
    # step back over that partial word so it isn't counted as context.
    while i > 0 and _is_letter(text[i - 1]):
        i -= 1

    # Keep scanning backward until we have n words or hit the start of text.
    while i > 0 and len(words) < n:
        c = text[i - 1]

        # Optional hard boundary: stop if we reach sentence-ending punctuation.
        if c in stop_at:
            break

        # Anything that isn't a letter (space, digit, comma, newline, ...)
        # just separates words, so skip over it.
        if not _is_letter(c):
            i -= 1
            continue

        # We've hit the LAST letter of a word. Remember where it ends,
        # then walk backward to find where the word begins.
        end = i
        while i > 0:
            p = text[i - 1]
            if _is_letter(p):
                # Still inside the word.
                i -= 1
            elif p in join and i >= 2 and _is_letter(text[i - 2]):
                # A joining char (like the apostrophe in "don't") with a
                # letter on BOTH sides: treat it as part of the word.
                i -= 1
            else:
                # Anything else marks the start of the word.
                break

        # Slice out the word, drop joining chars ("don't" -> "dont"),
        # and lowercase it so keyword matching is case-insensitive.
        word = "".join(ch for ch in text[i:end] if _is_letter(ch))
        words.append(word.lower())

    # We collected words from nearest to farthest; flip to reading order.
    return words[::-1]


def next_words(text: str, pos: int, n: int = 3, join: str = "'’", stop_at: str = "") -> list[str]:
    """
    Mirror of prev_words: return up to n words that come after position
    `pos` in `text`, scanning forward as far as needed.

    text     -- the full text being searched
    pos      -- where the regex match ends (e.g. m.end())
    n        -- how many words to collect
    join     -- characters allowed *inside* a word ("don't" -> "dont")
    stop_at  -- characters that end the search early, e.g. ".!?"

    Words are returned in reading order, so the nearest word is first.
    """
    words = []
    i = pos      # cursor; text[i] is always the next character to inspect
    size = len(text)

    # If the match ends in the middle of a word (e.g. "INV-1234abc"),
    # step forward over the partial word so it isn't counted as context.
    while i < size and _is_letter(text[i]):
        i += 1

    while i < size and len(words) < n:
        c = text[i]

        if c in stop_at:
            break

        if not _is_letter(c):
            i += 1
            continue

        # First letter of a word: walk forward to find where it ends.
        start = i
        while i < size:
            p = text[i]
            if _is_letter(p):
                i += 1
            elif p in join and i + 1 < size and _is_letter(text[i + 1]):
                # Joining char with a letter on BOTH sides (previous char
                # is a letter because we are inside a word).
                i += 1
            else:
                break

        word = "".join(ch for ch in text[start:i] if _is_letter(ch))
        words.append(word.lower())

    return words
