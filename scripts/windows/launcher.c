#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

#ifndef VBOT_ROLE
#define VBOT_ROLE L"python"
#endif

typedef int(__cdecl *py_bytes_main_fn)(int, char **);
typedef void(__cdecl *py_set_path_fn)(const wchar_t *);
typedef void(__cdecl *py_set_home_fn)(const wchar_t *);

static bool show_error_dialog = false;
static char *utf8(const wchar_t *value);

static void fail(const wchar_t *message) {
    /* Background roles must exit on startup failure, never wait on a hidden
       modal dialog. Console commands preserve their caller's error stream. */
    wchar_t detail[2048], executable[MAX_PATH];
    GetModuleFileNameW(NULL, executable, MAX_PATH);
    _snwprintf_s(detail, 2048, _TRUNCATE, L"[ERROR] %ls\nApplication: %ls\n", message, executable);
    char *bytes = utf8(detail);
    if (bytes != NULL) {
        DWORD written;
        WriteFile(GetStdHandle(STD_ERROR_HANDLE), bytes, (DWORD)strlen(bytes), &written, NULL);
        free(bytes);
    }
    if (show_error_dialog) MessageBoxW(NULL, message, L"vBot could not start", MB_OK | MB_ICONERROR);
}

static bool safe_basename(const wchar_t *value) {
    size_t length = wcslen(value);
    if (length == 0 || length > 128 || value[0] == L'.' || value[length - 1] == L'.') return false;
    for (size_t i = 0; i < length; ++i) {
        wchar_t c = value[i];
        if (!((c >= L'a' && c <= L'z') || (c >= L'0' && c <= L'9') ||
              c == L'_' || c == L'-')) return false;
    }
    return wcscmp(value, L".") != 0 && wcscmp(value, L"..") != 0;
}

static bool read_pointer(const wchar_t *root, wchar_t *version, size_t capacity) {
    wchar_t path[MAX_PATH];
    if (_snwprintf_s(path, MAX_PATH, _TRUNCATE, L"%ls\\active-version", root) < 0) return false;
    FILE *stream = NULL;
    if (_wfopen_s(&stream, path, L"rt, ccs=UTF-8") != 0 || stream == NULL) return false;
    bool ok = fgetws(version, (int)capacity, stream) != NULL;
    fclose(stream);
    if (!ok) return false;
    version[wcscspn(version, L"\r\n")] = L'\0';
    return safe_basename(version);
}

static bool executable_directory(wchar_t *path, size_t capacity) {
    DWORD length = GetModuleFileNameW(NULL, path, (DWORD)capacity);
    if (length == 0 || length >= capacity) return false;
    wchar_t *slash = wcsrchr(path, L'\\');
    if (slash == NULL) return false;
    *slash = L'\0';
    return true;
}

static char *utf8(const wchar_t *value) {
    int size = WideCharToMultiByte(CP_UTF8, 0, value, -1, NULL, 0, NULL, NULL);
    if (size <= 0) return NULL;
    char *result = (char *)malloc((size_t)size);
    if (result == NULL || WideCharToMultiByte(CP_UTF8, 0, value, -1, result, size, NULL, NULL) == 0) {
        free(result);
        return NULL;
    }
    return result;
}

static int run_python(const wchar_t *runtime, const wchar_t *app, int argc, wchar_t **argv,
                      bool payload_install) {
    wchar_t dll[MAX_PATH], zip[MAX_PATH], path[32768], pywin32[MAX_PATH];
    WIN32_FIND_DATAW found;
    HANDLE search;
    if (_snwprintf_s(dll, MAX_PATH, _TRUNCATE, L"%ls\\python3*.dll", runtime) < 0) return 111;
    search = FindFirstFileW(dll, &found);
    if (search == INVALID_HANDLE_VALUE) { fail(L"The private Python runtime is incomplete."); return 111; }
    while (_wcsicmp(found.cFileName, L"python3.dll") == 0) {
        if (!FindNextFileW(search, &found)) {
            FindClose(search);
            fail(L"The private Python runtime has no versioned Python DLL.");
            return 111;
        }
    }
    FindClose(search);
    if (_snwprintf_s(dll, MAX_PATH, _TRUNCATE, L"%ls\\%ls", runtime, found.cFileName) < 0) return 111;
    HMODULE python = LoadLibraryExW(dll, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (python == NULL) { fail(L"The private Python runtime could not be loaded."); return 111; }
    py_bytes_main_fn py_main = (py_bytes_main_fn)GetProcAddress(python, "Py_BytesMain");
    py_set_path_fn set_path = (py_set_path_fn)GetProcAddress(python, "Py_SetPath");
    py_set_home_fn set_home = (py_set_home_fn)GetProcAddress(python, "Py_SetPythonHome");
    if (py_main == NULL || set_path == NULL || set_home == NULL) { fail(L"The private Python runtime is incompatible."); return 111; }
    set_home(runtime);
    _snwprintf_s(pywin32, MAX_PATH, _TRUNCATE,
        L"%ls\\Lib\\site-packages\\pywin32_system32", runtime);
    if (GetFileAttributesW(pywin32) != INVALID_FILE_ATTRIBUTES) SetDllDirectoryW(pywin32);
    _snwprintf_s(zip, MAX_PATH, _TRUNCATE, L"%ls\\python*.zip", runtime);
    search = FindFirstFileW(zip, &found);
    if (search != INVALID_HANDLE_VALUE) {
        FindClose(search);
        _snwprintf_s(zip, MAX_PATH, _TRUNCATE, L"%ls\\%ls", runtime, found.cFileName);
    } else {
        _snwprintf_s(zip, MAX_PATH, _TRUNCATE, L"%ls\\python.zip", runtime);
    }
    _snwprintf_s(path, 32768, _TRUNCATE,
        L"%ls;%ls;%ls\\Lib;%ls\\DLLs;%ls\\Lib\\site-packages;"
        L"%ls\\Lib\\site-packages\\win32;%ls\\Lib\\site-packages\\win32\\lib;"
        L"%ls\\Lib\\site-packages\\Pythonwin;%ls",
        app, zip, runtime, runtime, runtime, runtime, runtime, runtime, pywin32);
    set_path(path);

    bool python_command = argc > 1 && (wcscmp(argv[1], L"-m") == 0 || wcscmp(argv[1], L"-c") == 0);
    bool generic = wcscmp(VBOT_ROLE, L"python") == 0 || python_command;
    const wchar_t *module = NULL;
    if (payload_install) module = L"cli.application.install";
    else if (!generic) {
        if (wcscmp(VBOT_ROLE, L"server") == 0) module = L"server.main";
        else if (wcscmp(VBOT_ROLE, L"desktop") == 0) module = L"desktop.main";
        else if (wcscmp(VBOT_ROLE, L"update") == 0) module = L"cli.application.worker";
        else if (wcscmp(VBOT_ROLE, L"host") == 0) module = argc == 1 ? L"cli.application.host" : L"cli.main";
    }
    int extra = (module != NULL ? 2 : 0) + 3;
    char **bytes = (char **)calloc((size_t)argc + (size_t)extra + 1, sizeof(char *));
    if (bytes == NULL) return 111;
    bytes[0] = utf8(argv[0]);
    int target = 1;
    bytes[target++] = _strdup("-I");
    bytes[target++] = _strdup("-S");
    bytes[target++] = _strdup("-B");
    if (module != NULL) { bytes[target++] = _strdup("-m"); bytes[target++] = utf8(module); }
    int first_argument = payload_install ? 3 : 1;
    for (int i = first_argument; i < argc; ++i) bytes[target++] = utf8(argv[i]);
    for (int i = 0; i < target; ++i) if (bytes[i] == NULL) return 111;
    int result = py_main(target, bytes);
    for (int i = 0; i < target; ++i) free(bytes[i]);
    free(bytes);
    return result;
}

static int vbot_main(void) {
    HINSTANCE instance = NULL, previous = NULL;
    PWSTR command_line = NULL;
    int show = 0;
    (void)instance; (void)previous; (void)command_line; (void)show;
    int argc = 0;
    wchar_t **argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    wchar_t root[MAX_PATH], version[129], runtime[MAX_PATH], app[MAX_PATH];
    if (argv == NULL || !executable_directory(root, MAX_PATH)) return 111;
    show_error_dialog = (wcscmp(VBOT_ROLE, L"host") == 0 && argc == 1) ||
                        wcscmp(VBOT_ROLE, L"desktop") == 0;
#ifdef VBOT_STABLE_BOOTSTRAP
    bool payload_install = argc >= 5 && wcscmp(argv[1], L"application") == 0 && wcscmp(argv[2], L"install") == 0;
    /* A no-argument bootstrap is the tray host. Detach only this child from a
       console inherited or allocated for it; command invocations keep normal
       console output and wait semantics. */
    if (argc == 1) FreeConsole();
    const wchar_t *payload = NULL;
    if (payload_install) for (int i = 3; i + 1 < argc; ++i) if (wcscmp(argv[i], L"--payload") == 0) payload = argv[i + 1];
    if (payload != NULL) {
        _snwprintf_s(app, MAX_PATH, _TRUNCATE, L"%ls\\app", payload);
        _snwprintf_s(runtime, MAX_PATH, _TRUNCATE, L"%ls\\runtime", payload);
    } else {
        if (!read_pointer(root, version, 129)) { fail(L"vBot has no valid active version."); return 111; }
        _snwprintf_s(app, MAX_PATH, _TRUNCATE, L"%ls\\versions\\%ls\\app", root, version);
        _snwprintf_s(runtime, MAX_PATH, _TRUNCATE, L"%ls\\versions\\%ls\\runtime", root, version);
    }
#else
    wchar_t *runtime_leaf = wcsrchr(root, L'\\');
    if (runtime_leaf == NULL || _wcsicmp(runtime_leaf + 1, L"runtime") != 0) return 111;
    *runtime_leaf = L'\0';
    _snwprintf_s(app, MAX_PATH, _TRUNCATE, L"%ls\\app", root);
    _snwprintf_s(runtime, MAX_PATH, _TRUNCATE, L"%ls\\runtime", root);
#endif
    int result = run_python(runtime, app, argc, argv,
#ifdef VBOT_STABLE_BOOTSTRAP
        payload_install
#else
        false
#endif
    );
    LocalFree(argv);
    return result;
}

int wmain(void) { return vbot_main(); }

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR command_line, int show) {
    (void)instance; (void)previous; (void)command_line; (void)show;
    return vbot_main();
}
