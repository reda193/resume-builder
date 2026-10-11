import re
from itertools import groupby

MIN_GUTTER_WIDTH = 8       
MAX_CROSSING_LINES = 3     
MIN_COLUMN_LINES = 5       
MIN_COLUMN_SHARE = 0.20    
SAME_EDGE_SHARE = 0.7      
WORD_SPACE = 6             

def group_into_lines(words, tolerance=3):
    lines = []
    for word in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(word["top"] - lines[-1][0]["top"]) <= tolerance:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def segments(line):
    pieces = []
    for word in line:
        # Bigger text has bigger spaces, so allow for the word's height (its font size).
        allowed = max(WORD_SPACE, 0.4 * (word["bottom"] - word["top"]))
        if pieces and word["x0"] - pieces[-1][1] <= allowed:
            pieces[-1][1] = word["x1"]
        else:
            pieces.append([word["x0"], word["x1"]])
    return pieces


def crosses(line, x):
    """True if a solid piece of this line's text covers position x."""
    return any(start < x < end for start, end in segments(line))


def line_text(line):
    return " ".join(word["text"] for word in line)


def char_count(lines):
    return sum(len(word["text"]) for line in lines for word in line)


def candidate_gutters(lines, page_width):
    start = int(page_width * 0.2)
    xs = range(start, int(page_width * 0.8))
    crossing = [sum(1 for line in lines if crosses(line, x)) for x in xs]

    runs = []
    position = 0
    for is_open, group in groupby(crossing, key=lambda count: count <= MAX_CROSSING_LINES):
        length = len(list(group))
        if is_open and length >= MIN_GUTTER_WIDTH:
            runs.append((position, position + length))
        position += length

    # Inside each run, put the gutter in the middle of its emptiest stretch.
    gutters = []
    for run_start, run_end in sorted(runs, key=lambda run: run[0] - run[1]):
        fewest = min(crossing[run_start:run_end])
        best_start, best_length, i = run_start, 0, run_start
        for count, group in groupby(crossing[run_start:run_end]):
            length = len(list(group))
            if count == fewest and length > best_length:
                best_start, best_length = i, length
            i += length
        gutters.append(start + best_start + best_length / 2)
    return gutters


def looks_like_columns(lines, gutter):
    one_side = [word for line in lines if not crosses(line, gutter) for word in line]
    left = group_into_lines([w for w in one_side if w["x1"] <= gutter])
    right = group_into_lines([w for w in one_side if w["x0"] >= gutter])

    right_margin = max(word["x1"] for line in lines for word in line)
    left_rows = [line[0]["top"] for line in left]
    right = [line for line in right
             if not (line[-1]["x1"] >= right_margin - 2
                     and any(abs(line[0]["top"] - top) <= 3 for top in left_rows))]

    if len(left) < MIN_COLUMN_LINES or len(right) < MIN_COLUMN_LINES:
        return False
    total = char_count(lines)
    if char_count(left) < total * MIN_COLUMN_SHARE or char_count(right) < total * MIN_COLUMN_SHARE:
        return False

    ends = [round(line[-1]["x1"]) for line in right]
    most_at_one_spot = max(sum(1 for e in ends if abs(e - spot) <= 1) for spot in ends)
    if most_at_one_spot >= len(ends) * SAME_EDGE_SHARE:
        return False
    return True


def find_gutter(lines, page_width):
    """The x position between two columns, or None for a one-column page."""
    for gutter in candidate_gutters(lines, page_width):
        if looks_like_columns(lines, gutter):
            return gutter
    return None



def page_text(page):
    """Text of one PDF page. A two-column page is read as: the header (lines spanning
    both columns, or sitting above them, like the name), then the whole main column,
    then the whole sidebar."""
    words = page.extract_words()
    if not words:
        return ""
    lines = group_into_lines(words)
    gutter = find_gutter(lines, page.width)

    if gutter is None:
        text = page.extract_text() or ""
    else:
        spanning = [line for line in lines if crosses(line, gutter)]
        rest = [word for line in lines if not crosses(line, gutter) for word in line]
        left = group_into_lines([w for w in rest if w["x1"] <= gutter])
        right = group_into_lines([w for w in rest if w["x0"] >= gutter])
        if left and right:
            columns_start = max(left[0][0]["top"], right[0][0]["top"]) - 2
            header = [line for line in left + right if line[0]["top"] < columns_start]
            header.sort(key=lambda line: (line[0]["top"], line[0]["x0"]))
            left = [line for line in left if line not in header]
            right = [line for line in right if line not in header]
        else:
            header = []

        main, side = (left, right) if char_count(left) >= char_count(right) else (right, left)
        top = sorted(spanning + header, key=lambda line: line[0]["top"])
        text = "\n".join(line_text(line) for line in top + main + side)

    # Some PDFs turn bullet symbols into codes like "(cid:127)". Remove them.
    return re.sub(r"\(cid:\d+\)\s*", "", text)