import {
  ApiClientError,
  RPC_ERROR_INVALID_CLIENT_REQUEST,
  rpc,
} from './transport.js';

// One directory of the server's filesystem, for path pickers. `path` null
// lists the places to start from (filesystem roots plus `home`); otherwise an
// absolute path, `~` or `~/...`, or with `root` a path relative to that root
// ('' is the root itself). `include_files` adds files to the directories;
// `prefix` keeps only entries whose names start with it, ignoring case.
// The params keep the RPC's own shape so an isolated Extension page can pass
// its host bridge as the same listing function.
export function listServerDirectory(
  {
    path = null,
    root,
    include_files: includeFiles = false,
    prefix = null,
  } = {},
  options = {},
) {
  if (path !== null && typeof path !== 'string') {
    throw invalidListing('Directory path must be a string or null');
  }
  if (root !== undefined && (typeof root !== 'string' || root === '')) {
    throw invalidListing('Directory root must be a non-empty string');
  }
  if (prefix !== null && typeof prefix !== 'string') {
    throw invalidListing('Name prefix must be a string or null');
  }
  const params = { path };
  if (root !== undefined) params.root = root;
  if (includeFiles) params.include_files = true;
  if (prefix) params.prefix = prefix;
  return rpc('filesystem.list', params, options);
}

// Creates the folder `name` inside the directory `path` (the same forms as a
// listing's `path`, with `root` relative to it) and resolves to `{path}`, the
// new folder's path in that form. The params keep the RPC's own shape too.
export function createServerDirectory({ path, name, root } = {}, options = {}) {
  const method = 'filesystem.create_directory';
  if (typeof path !== 'string') {
    throw invalidRequest(method, 'Directory path must be a string');
  }
  if (typeof name !== 'string' || name === '') {
    throw invalidRequest(method, 'Folder name must be a non-empty string');
  }
  if (root !== undefined && (typeof root !== 'string' || root === '')) {
    throw invalidRequest(method, 'Directory root must be a non-empty string');
  }
  const params = { path, name };
  if (root !== undefined) params.root = root;
  return rpc(method, params, options);
}

function invalidListing(message) {
  return invalidRequest('filesystem.list', message);
}

function invalidRequest(method, message) {
  return new ApiClientError(RPC_ERROR_INVALID_CLIENT_REQUEST, message, {
    method,
  });
}
