/**
 * QJL-Inspired 1-bit Sketch-based Ghost Token Detection.
 *
 * Implements a lightweight sketching approach inspired by TurboQuant's
 * Quantized Johnson-Lindenstrauss (QJL) technique. Instead of operating
 * on high-dimensional key vectors, we apply the same principle to text:
 * project into a compact binary sketch via randomized hashing, then use
 * Hamming distance to approximate similarity.
 *
 * This enables O(1) per-pair similarity checks for clustering near-duplicate
 * messages and detecting "ghost" runs — runs that load context but produce
 * negligible output.
 */
/**
 * Compute a 1-bit sketch of the given text.
 *
 * Each word is hashed and folded into a fixed-width bit vector using XOR
 * with rotated hash values. The result is a compact binary fingerprint
 * that preserves approximate similarity under Hamming distance.
 *
 * @param text - The input text to sketch
 * @param dimensions - Number of bits in the sketch (must be a multiple of 8)
 * @returns A Uint8Array representing the binary sketch
 */
export declare function computeSketch(text: string, dimensions?: number): Uint8Array;
/**
 * Compute Hamming similarity between two sketches.
 *
 * Returns a value in [0, 1] where 1 means identical sketches.
 * Similarity = 1 - (hammingDistance / totalBits).
 *
 * @param a - First sketch
 * @param b - Second sketch (must be same length as a)
 * @returns Similarity score between 0 and 1
 */
export declare function sketchSimilarity(a: Uint8Array, b: Uint8Array): number;
/**
 * Cluster items by sketch similarity using single-linkage clustering.
 *
 * Items whose sketch similarity exceeds the threshold are placed in the
 * same cluster. Returns an array of clusters, where each cluster is an
 * array of item IDs.
 *
 * @param items - Array of objects with id and text fields
 * @param threshold - Minimum similarity to join a cluster (0-1, default 0.85)
 * @returns Array of clusters (each cluster is an array of item IDs)
 */
export declare function clusterBySketch(items: Array<{
    id: string;
    text: string;
}>, threshold?: number): Array<Array<string>>;
//# sourceMappingURL=jl-sketcher.d.ts.map