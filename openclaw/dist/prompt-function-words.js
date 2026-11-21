"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.PROMPT_FUNCTION_WORDS = void 0;
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
exports.PROMPT_FUNCTION_WORDS = new Set([
    // English
    "this", "that", "these", "those", "them", "they", "their", "theirs", "there",
    "then", "than", "what", "when", "where", "which", "while", "whom", "whose",
    "with", "without", "within", "would", "should", "could", "shall", "might",
    "must", "have", "been", "being", "were", "does", "done",
    "your", "yours", "ours", "ourselves", "myself", "yourself", "itself",
    "also", "just", "very", "more", "most", "some", "such", "only", "into",
    "from", "about", "over", "here", "really", "still", "even", "each", "other",
    "both", "same", "will", "because", "though", "although", "whether",
    "much", "many", "ever", "never", "maybe", "again", "once",
    // Portuguese (accented and unaccented forms)
    "esse", "esses", "essa", "essas", "isso", "este", "estes", "esta", "estas",
    "isto", "aquele", "aquela", "aqueles", "aquelas", "aquilo", "nosso", "nossa",
    "nossos", "nossas", "seus", "suas", "meus", "minha",
    "minhas", "para", "como", "temos", "tenho", "tinha", "tenha", "tambem",
    "também", "está", "estou", "estão", "estao", "estamos", "mais",
    "muito", "muita", "muitos", "muitas", "pois", "porque", "porém", "porem",
    "entao", "então", "quando", "onde", "qual", "quais", "quem", "aqui", "ainda",
    "apenas", "sobre", "entre", "sido", "será", "sera", "seria", "deve", "devem",
    "pode", "podem", "poderia", "havia", "mesmo", "nunca", "sempre", "depois",
    "agora", "tudo", "toda", "todos", "todas",
]);
//# sourceMappingURL=prompt-function-words.js.map