import { readFile } from "node:fs/promises";
import { getSanitizedTypeScriptVersion } from "./getSanitizedTypeScriptVersion";
import { getTypeScriptPackageJsonPaths } from "./getTypeScriptPackageJsonPaths";
let tscVersion;
export const getTypeScriptUserAgentPair = async () => {
    if (tscVersion === null) {
        return undefined;
    }
    else if (typeof tscVersion === "string") {
        return ["md/tsc", tscVersion];
    }
    const dirname = typeof __dirname !== "undefined" ? __dirname : undefined;
    for (const typescriptPackageJsonPath of getTypeScriptPackageJsonPaths(dirname)) {
        try {
            const packageJson = await readFile(typescriptPackageJsonPath, "utf-8");
            const { version } = JSON.parse(packageJson);
            const sanitizedVersion = getSanitizedTypeScriptVersion(version);
            if (typeof sanitizedVersion !== "string") {
                continue;
            }
            tscVersion = sanitizedVersion;
            return ["md/tsc", tscVersion];
        }
        catch {
        }
    }
    tscVersion = null;
    return undefined;
};
