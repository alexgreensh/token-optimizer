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

// ---------------------------------------------------------------------------
// Core sketch operations
// ---------------------------------------------------------------------------

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
export function computeSketch(text: string, dimensions: number = 64): Uint8Array {
  const byteLen = Math.ceil(dimensions / 8);
  const sketch = new Uint8Array(byteLen);

  const words = text.toLowerCase().split(/\s+/).filter((w) => w.length > 0);
  if (words.length === 0) return sketch;

  for (const word of words) {
    const h = fnv1aHash(word);
    // Fold hash into sketch via XOR with bit-rotated variants
    for (let i = 0; i < byteLen; i++) {
      const rotated = rotateRight32(h, (i * 7 + 3) % 32);
      sketch[i] ^= (rotated >>> (i % 4) * 8) & 0xff;
    }
  }

  return sketch;
}

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
export function sketchSimilarity(a: Uint8Array, b: Uint8Array): number {
  if (a.length !== b.length) {
    throw new Error(
      `Sketch length mismatch: ${a.length} vs ${b.length}`
    );
  }

  const totalBits = a.length * 8;
  if (totalBits === 0) return 1;

  let hammingDist = 0;
  for (let i = 0; i < a.length; i++) {
    hammingDist += popcount8(a[i] ^ b[i]);
  }

  return 1 - hammingDist / totalBits;
}

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
export function clusterBySketch(
  items: Array<{ id: string; text: string }>,
  threshold: number = 0.85
): Array<Array<string>> {
  if (items.length === 0) return [];

  if (items.length > 2000) {
    console.warn(`clusterBySketch: ${items.length} items exceeds recommended maximum of 2000. Consider using simpler grouping.`);
  }

  // Compute sketches for all items
  const sketches = items.map((item) => computeSketch(item.text));

  // Union-Find for clustering
  const parent = items.map((_, i) => i);

  function find(x: number): number {
    while (parent[x] !== x) {
      parent[x] = parent[parent[x]]; // path compression
      x = parent[x];
    }
    return x;
  }

  function union(x: number, y: number): void {
    const rx = find(x);
    const ry = find(y);
    if (rx !== ry) parent[rx] = ry;
  }

  // Compare all pairs (O(n^2), acceptable for typical run counts)
  for (let i = 0; i < items.length; i++) {
    for (let j = i + 1; j < items.length; j++) {
      if (sketchSimilarity(sketches[i], sketches[j]) >= threshold) {
        union(i, j);
      }
    }
  }

  // Collect clusters
  const clusterMap = new Map<number, string[]>();
  for (let i = 0; i < items.length; i++) {
    const root = find(i);
    if (!clusterMap.has(root)) clusterMap.set(root, []);
    clusterMap.get(root)!.push(items[i].id);
  }

  return Array.from(clusterMap.values());
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

/**
 * FNV-1a 32-bit hash for a string.
 * Fast, well-distributed hash suitable for sketch construction.
 */
function fnv1aHash(str: string): number {
  let hash = 0x811c9dc5; // FNV offset basis
  for (let i = 0; i < str.length; i++) {
    hash ^= str.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193); // FNV prime
  }
  return hash >>> 0; // ensure unsigned
}

/** Rotate a 32-bit integer right by n positions. */
function rotateRight32(value: number, n: number): number {
  n = n & 31;
  return ((value >>> n) | (value << (32 - n))) >>> 0;
}

/** Count set bits in an 8-bit value. */
function popcount8(byte: number): number {
  byte = byte - ((byte >>> 1) & 0x55);
  byte = (byte & 0x33) + ((byte >>> 2) & 0x33);
  return (byte + (byte >>> 4)) & 0x0f;
}
