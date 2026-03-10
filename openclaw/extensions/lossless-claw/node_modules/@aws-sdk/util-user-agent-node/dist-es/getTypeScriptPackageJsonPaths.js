import { join, normalize, sep } from "node:path";
const typescriptPackageJsonPath = join("node_modules", "typescript", "package.json");
export const getTypeScriptPackageJsonPaths = (dirname) => {
    const cwdPath = join(process.cwd(), typescriptPackageJsonPath);
    if (!dirname) {
        return [cwdPath];
    }
    const normalizedPath = normalize(dirname);
    const parts = normalizedPath.split(sep);
    const nodeModulesIndex = parts.indexOf("node_modules");
    const parentDir = nodeModulesIndex !== -1 ? parts.slice(0, nodeModulesIndex).join(sep) : dirname;
    const parentDirPath = join(parentDir, typescriptPackageJsonPath);
    if (cwdPath === parentDirPath) {
        return [cwdPath];
    }
    return [parentDirPath, cwdPath];
};
