import {
  ApiClientError,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  rpc,
} from './transport.js';

// One directory of the server's filesystem, for path pickers. `path` null
// lists the places to start from (filesystem roots plus `home`); otherwise an
// absolute path, `~` or `~/...`, or with `root` a path relative to that root
// ('' is the root itself). `include_files` adds files to the directories.
// The params keep the RPC's own shape so an isolated Extension page can pass
// its host bridge as the same listing function.
export function listServerDirectory(
  { path = null, root, include_files: includeFiles = false } = {},
  options = {},
) {
  if (path !== null && typeof path !== 'string') {
    throw invalidListing('Directory path must be a string or null');
  }
  if (root !== undefined && (typeof root !== 'string' || root === '')) {
    throw invalidListing('Directory root must be a non-empty string');
  }
  const params = { path };
  if (root !== undefined) params.root = root;
  if (includeFiles) params.include_files = true;
  return rpc('filesystem.list', params, options);
}

function invalidListing(message) {
  return new ApiClientError(RPC_ERROR_INVALID_CLIENT_REQUEST, message, {
    method: 'filesystem.list',
  });
}
