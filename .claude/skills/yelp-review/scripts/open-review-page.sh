#!/usr/bin/env bash
# Draft-to-clipboard handoff for the yelp-review skill.
#
# Opens Yelp's search results for a business and puts the draft on the
# clipboard, so the remaining steps are: click the business, click Write a
# Review, paste, read it, post.
#
# Usage:
#   open-review-page.sh "Restaurant Name" "City" [draft-file]
#   cat draft.txt | open-review-page.sh "Restaurant Name" "City"
#
# Deliberately stops short of submitting. See the Posting section of SKILL.md.

set -euo pipefail

if [ $# -lt 2 ]; then
    echo "usage: $(basename "$0") \"Restaurant Name\" \"City\" [draft-file]" >&2
    exit 64
fi

name=$1
city=$2
draft_file=${3:-}

urlencode() {
    local s=$1 out= i c
    for (( i = 0; i < ${#s}; i++ )); do
        c=${s:i:1}
        case $c in
            [a-zA-Z0-9.~_-]) out+=$c ;;
            ' ')             out+='+' ;;
            *)               printf -v c '%%%02X' "'$c"; out+=$c ;;
        esac
    done
    printf '%s' "$out"
}

url="https://www.yelp.com/search?find_desc=$(urlencode "$name")&find_loc=$(urlencode "$city")"

# Read the draft from a file, or from stdin when it is piped in.
draft=''
if [ -n "$draft_file" ]; then
    [ -r "$draft_file" ] || { echo "cannot read draft file: $draft_file" >&2; exit 66; }
    draft=$(cat -- "$draft_file")
elif [ ! -t 0 ]; then
    draft=$(cat)
fi

if [ -n "$draft" ]; then
    if   command -v pbcopy    >/dev/null 2>&1; then printf '%s' "$draft" | pbcopy
    elif command -v wl-copy   >/dev/null 2>&1; then printf '%s' "$draft" | wl-copy
    elif command -v xclip     >/dev/null 2>&1; then printf '%s' "$draft" | xclip -selection clipboard
    elif command -v clip.exe  >/dev/null 2>&1; then printf '%s' "$draft" | clip.exe
    else
        echo "No clipboard tool found (pbcopy/wl-copy/xclip/clip.exe). Draft:" >&2
        printf '\n%s\n\n' "$draft" >&2
        draft=''
    fi
    [ -n "$draft" ] && echo "Draft copied to clipboard."
fi

echo "Opening: $url"
if   command -v open        >/dev/null 2>&1; then open "$url"
elif command -v xdg-open    >/dev/null 2>&1; then xdg-open "$url"
elif command -v wslview     >/dev/null 2>&1; then wslview "$url"
else echo "Open it manually." >&2
fi

echo "Next: click the business -> Write a Review -> paste -> read it once -> post."
