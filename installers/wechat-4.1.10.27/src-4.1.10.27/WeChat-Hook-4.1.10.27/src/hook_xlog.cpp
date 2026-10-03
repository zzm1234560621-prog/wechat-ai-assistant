// ============================================================================
// xlog 明文捕获 —— 为了在**加密之前**拿到微信自己的日志
// ============================================================================
//
// 为什么需要它：微信盘上的 .xlog 是 mars 的 MAGIC_COMPRESS_START2（首字节 0x07），
// 该格式头部带 64 字节 RSA 加密的密钥，**没有微信私钥解不开**（mars 官方脚本在
// 0x07 分支里直接写着 "use wrong decode script"）。所以唯一能读日志的地方，就是
// mars xlog 写入路径上、"正文还没被加密"的那一刻。本文件就插在那里。
//
// 怎么插：对几个候选写入函数做 inline hook（Hook_Inline 会保存全部寄存器、
// 调 handler、再执行被偷的原始字节并跳回，所以 handler 里看到的就是**函数入口的
// 原始参数**）。我们不去猜哪个寄存器是正文，而是把几种可能的解释都试一遍，
// 只写"确实像一行日志"的那个结果。每个 hook 单独一个文件，事后一眼就能看出
// 哪种解释命中了。
//
// 三条硬约束（照项目的既有铁律）：
//   1. **默认关**：只有 %TEMP%\wx_xlog_capture.on 存在才安装，发布包行为不变。
//   2. **绝不在 hook 里做任何会再触发日志的事**（否则递归）。只用裸 Win32 文件 API，
//      配一个线程内的递归保护。
//   3. **有硬上限**：单文件封顶 + 总量封顶，写满就停，绝不让它把磁盘写爆。
//
// 代价说清楚：这个函数会被调用得极其频繁，加 hook 会让微信稍慢；上限一到就
// 自动停止写入，不会再拖。

#include <Windows.h>
#include <cstdint>
#include <cstdio>
#include <cstring>

#include "global.h"
#include "Hook_Method.h"
#include "hook_xlog.h"

namespace hook {

namespace {

// ---------------- 上限 ----------------
constexpr size_t kMaxLineBytes   = 4096;              // 单行最多认这么多
constexpr size_t kFlushBytes     = 64 * 1024;         // 缓冲
constexpr size_t kMaxFileBytes   = 64ull * 1024 * 1024;   // 单个 hook 封顶 64MB
constexpr size_t kMaxTotalBytes  = 256ull * 1024 * 1024;  // 全部加起来封顶 256MB

// ---------------- 状态 ----------------
struct Capture
{
    const char* name;          // 文件名后缀，同时标识 hook
    HANDLE      file;
    CRITICAL_SECTION cs;
    size_t      written;
    size_t      buf_len;
    char        buf[kFlushBytes];
    unsigned long long lines;
    unsigned long long skipped;
    bool        active;
};

enum
{
    HK_APPENDER_WRITE = 0,     // rva 0x107d10  mars appender 写入（带递归保护的那个）
    HK_XLOGGER_OP,             // rva 0x68fa0   XLogger::operator()
    HK_TYPESAFE_FORMAT,        // rva 0x69160   XLogger::DoTypeSafeFormat
    HK_ROLNAME_A,              // rva 0x938d0   引用 _%04d%02d%02d.xlog
    HK_ROLNAME_B,              // rva 0x94730
    HK_ROLNAME_C,              // rva 0x954a0
    HK_COUNT
};

Capture g_cap[HK_COUNT] = {};
bool    g_installed = false;
size_t  g_total = 0;

// 递归保护：hook 里万一又触发了 xlog，直接放行。
// ⚠️ 故意**不用 RAII**：下面要在同一个函数里用 __try/__except，
// 而 MSVC 禁止"需要对象展开"的函数使用 SEH。
thread_local int g_in_hook = 0;

// ---------------- 内存安全读取 ----------------
// 只信"确实提交了、且不跨页"的读法。读不到就放弃，**绝不硬读**（会把微信读崩）。
bool Readable(const void* p, size_t n)
{
    if (!p || !n)
        return false;

    MEMORY_BASIC_INFORMATION mbi = {};
    if (VirtualQuery(p, &mbi, sizeof(mbi)) == 0)
        return false;
    if (mbi.State != MEM_COMMIT)
        return false;
    if (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD))
        return false;

    const char* base  = (const char*)mbi.BaseAddress;
    const char* cur   = (const char*)p;
    size_t available  = (size_t)(base + mbi.RegionSize - cur);
    return available >= n;
}

// 读一段文本：given>0 表示已知长度；given==0 表示按 C 串读到 NUL。
size_t ReadText(const void* p, size_t given, char* out, size_t cap)
{
    if (!p || cap == 0)
        return 0;

    size_t n = 0;
    if (given)
    {
        if (given > kMaxLineBytes || !Readable(p, given))
            return 0;
        n = given < (cap - 1) ? given : (cap - 1);
        memcpy(out, p, n);
    }
    else
    {
        if (!Readable(p, 1))
            return 0;
        const char* s = (const char*)p;
        while (n + 1 < cap && n < kMaxLineBytes && Readable(s + n, 1))
        {
            char c = s[n];
            if (c == 0)
                break;
            out[n++] = c;
        }
    }
    out[n] = 0;
    return n;
}

// ---------------- 判据：这看起来像一行日志吗 ----------------
// 只在**通过**时才落盘。这个函数调用极频繁，所以判据要便宜且保守。
bool LooksLikeLog(const char* s, size_t n)
{
    if (n < 8 || n > kMaxLineBytes)
        return false;

    size_t printable = 0;
    for (size_t i = 0; i < n; ++i)
    {
        unsigned char c = (unsigned char)s[i];
        if (c == 9 || c == 10 || c == 13 || (c >= 32 && c < 127) || c >= 0x80)
            ++printable;
    }
    if (printable * 100 / n < 95)
        return false;

    // 真日志里必然出现其一。纯随机字节很难同时满足上面两条又被这里放过。
    for (size_t i = 0; i < n; ++i)
    {
        char c = s[i];
        if (c == ' ' || c == ':' || c == '[' || c == '/')
            return true;
    }
    return false;
}

// ---------------- 落盘 ----------------
void Flush(Capture& c)
{
    if (!c.file || c.buf_len == 0)
    {
        c.buf_len = 0;
        return;
    }
    if (c.written + c.buf_len > kMaxFileBytes)
    {
        c.buf_len = 0;
        return;
    }
    DWORD wrote = 0;
    ::WriteFile(c.file, c.buf, (DWORD)c.buf_len, &wrote, nullptr);
    c.written += wrote;
    c.buf_len = 0;
}

void Push(Capture& c, const char* s, size_t n)
{
    if (!c.active || !c.file || !n)
        return;
    if (c.written >= kMaxFileBytes || g_total >= kMaxTotalBytes)
        return;
    if (n + 2 > kFlushBytes)
        return;

    // 去掉尾部换行：落盘时统一补一个
    while (n && (s[n - 1] == '\n' || s[n - 1] == '\r'))
        --n;
    if (!n)
        return;

    if (c.buf_len + n + 1 > kFlushBytes)
        Flush(c);

    memcpy(c.buf + c.buf_len, s, n);
    c.buf_len += n;
    c.buf[c.buf_len++] = '\n';
    g_total += n + 1;
    ++c.lines;

    if (c.buf_len + 512 >= kFlushBytes)
        Flush(c);
}

// ---------------- 核心：从寄存器状态里把正文掏出来 ----------------
// ⚠️ 读别人进程的内存存在 TOCTOU 竞态（Readable() 检查完、memcpy 之前，那块内存
// 可能刚好被释放），所以真正的读取整体包在 SEH 里 —— **把微信读崩的代价是重新
// 扫码登录**，不值得赌。本函数只有 POD 局部量，不触发"需要对象展开"的限制。
bool ExtractLocked(::CALL_CONTEXT* ctx, char* line, size_t cap, size_t* out_len)
{
    const uint64_t regs[4] = { ctx->rcx, ctx->rdx, ctx->r8, ctx->r9 };

    // ① 寄存器直接指向 C 串。
    //    最可能的是 r8：mars `XloggerAppender::Write(this, info, log)` 的 log，
    //    与 0x107d10 序言里的 `mov rsi, r8` 吻合。
    const int cstr_order[4] = { 2, 1, 0, 3 };   // r8, rdx, rcx, r9
    for (int k = 0; k < 4; ++k)
    {
        size_t n = ReadText((const void*)regs[cstr_order[k]], 0, line, cap);
        if (LooksLikeLog(line, n))
        {
            *out_len = n;
            return true;
        }
    }

    // ② 寄存器指向 {ptr, ?, len} 结构 —— 保留旧 MyCallHandler_xLog 的解释。
    const int struct_order[3] = { 0, 1, 2 };    // rcx, rdx, r8
    for (int k = 0; k < 3; ++k)
    {
        const void* sp = (const void*)regs[struct_order[k]];
        if (!Readable(sp, sizeof(uint64_t) * 3))
            continue;
        const uint64_t* q = (const uint64_t*)sp;
        const uint64_t ptr = q[0];
        const uint64_t len = q[2];
        if (len == 0 || len > kMaxLineBytes)
            continue;
        size_t n = ReadText((const void*)ptr, (size_t)len, line, cap);
        if (LooksLikeLog(line, n))
        {
            *out_len = n;
            return true;
        }
    }
    return false;
}

void HandleOne(int id, ::CALL_CONTEXT* ctx)
{
    if (!ctx || id < 0 || id >= HK_COUNT)
        return;

    Capture& c = g_cap[id];
    if (!c.active || c.written >= kMaxFileBytes || g_total >= kMaxTotalBytes)
        return;
    if (g_in_hook)
        return;                       // 递归保护

    ++g_in_hook;

    char line[kMaxLineBytes + 1];
    size_t n = 0;
    bool got = false;

    __try
    {
        got = ExtractLocked(ctx, line, sizeof(line), &n);
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        got = false;
        ++c.skipped;
    }

    if (got)
        Push(c, line, n);

    --g_in_hook;
}

// 每个 hook 一个薄 handler（Hook_Inline 只收一个参数）
void Handler0(::CALL_CONTEXT* ctx) { HandleOne(HK_APPENDER_WRITE,  ctx); }
void Handler1(::CALL_CONTEXT* ctx) { HandleOne(HK_XLOGGER_OP,      ctx); }
void Handler2(::CALL_CONTEXT* ctx) { HandleOne(HK_TYPESAFE_FORMAT, ctx); }
void Handler3(::CALL_CONTEXT* ctx) { HandleOne(HK_ROLNAME_A,       ctx); }
void Handler4(::CALL_CONTEXT* ctx) { HandleOne(HK_ROLNAME_B,       ctx); }
void Handler5(::CALL_CONTEXT* ctx) { HandleOne(HK_ROLNAME_C,       ctx); }

struct Target
{
    uint64_t              rva;      // 相对 Weixin.dll
    size_t                hook_len; // **必须落在指令边界上**（已用反汇编核过）
    CallHookHandler       handler;
    const char*           name;
};

Target g_targets[HK_COUNT] = {
    { 0x107D10, 5, Handler0, "01_appender_write"  },
    { 0x068FA0, 5, Handler1, "02_xlogger_operator" },
    { 0x069160, 6, Handler2, "03_typesafe_format"  },
    { 0x0938D0, 5, Handler3, "04_rolname_a"        },
    { 0x094730, 5, Handler4, "05_rolname_b"        },
    { 0x0954A0, 5, Handler5, "06_rolname_c"        },
};

bool MarkerExists()
{
    // 默认关：只有这个标记文件存在才装 hook。
    // 放在 %TEMP%，不往微信安装目录写（那需要管理员）。
    wchar_t tmp[MAX_PATH] = {};
    if (GetTempPathW(MAX_PATH, tmp) == 0)
        return false;
    wchar_t flag[MAX_PATH] = {};
    _snwprintf_s(flag, _TRUNCATE, L"%swx_xlog_capture.on", tmp);
    return GetFileAttributesW(flag) != INVALID_FILE_ATTRIBUTES;
}

} // namespace

// ---------------- 兼容旧名字（头文件里声明的那个） ----------------
void MyCallHandler_xLog(::CALL_CONTEXT* ctx)
{
    Handler0(ctx);
}

// ---------------- 安装 ----------------
bool InstallXlogCapture()
{
    if (g_installed)
        return true;
    if (!g_hWeixinDll)
        return false;
    if (!MarkerExists())
        return false;          // 默认关

    wchar_t tmp[MAX_PATH] = {};
    if (GetTempPathW(MAX_PATH, tmp) == 0)
        return false;

    wchar_t dir[MAX_PATH] = {};
    _snwprintf_s(dir, _TRUNCATE, L"%swx_xlog_cap", tmp);
    ::CreateDirectoryW(dir, nullptr);

    int ok = 0;
    for (int i = 0; i < HK_COUNT; ++i)
    {
        Capture& c = g_cap[i];
        c.name = g_targets[i].name;

        wchar_t path[MAX_PATH] = {};
        _snwprintf_s(path, _TRUNCATE, L"%s\\%S.txt", dir, g_targets[i].name);

        ::InitializeCriticalSection(&c.cs);
        c.file = ::CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, nullptr,
                               CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (c.file == INVALID_HANDLE_VALUE)
        {
            c.file = nullptr;
            continue;
        }
        c.active = true;

        void* addr = (void*)((uintptr_t)g_hWeixinDll + g_targets[i].rva);
        if (Hook_Inline(addr, g_targets[i].hook_len, g_targets[i].handler))
            ++ok;
        else
            c.active = false;
    }

    g_installed = (ok > 0);

#ifdef _DEBUG
    char msg[160] = {};
    sprintf_s(msg, "[xlog-capture] installed %d/%d hooks, dir=%S\n", ok, (int)HK_COUNT, dir);
    OutputDebugStringA(msg);
#else
    (void)ok;
#endif
    return g_installed;
}

} // namespace hook
