/**
 * Prompt function words (ported from the Python _PROMPT_FUNCTION_WORDS in
 * _measure/continuity.py).
 *
 * Function words (EN + PT, len>3 so they survive the topic-token floor) that carry no
 * topic. Only the PROMPT side of keywordRelevanceScore drops them: a prompt such as
 * "would this benchmark spend our tokens" must not match an unrelated checkpoint on
 * "this"/"would" alone. Content words ("tokens", "build", "test") are deliberately absent.
 *
 * Kept in its own module because continuity.ts is a legacy over-limit file that may
 * only shrink (scripts/check_file_size.py).
 */
export declare const PROMPT_FUNCTION_WORDS: Set<string>;
//# sourceMappingURL=prompt-function-words.d.ts.map