/**
 * Returns candidate paths to the TypeScript package.json file derived from the current
 * working directory and, if provided, from the given directory.
 *
 * @param dirname - Optional directory path to derive an additional candidate path from.
 * @returns An array of unique candidate paths to the TypeScript package.json file.
 *
 * @internal
 */
export declare const getTypeScriptPackageJsonPaths: (dirname?: string) => string[];
