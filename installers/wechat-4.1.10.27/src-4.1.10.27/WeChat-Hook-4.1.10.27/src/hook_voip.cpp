// 语音通话链路的**多目标静默抓取**。
//
// ============================ 事故史（必读）============================
// 第一版把微信**搞崩了**：处理函数里用了 CRT 的 `_vsnprintf_s`，
// 转储显示崩在 `ucrtbase!__stdio_common_vsnprintf_s + 0x2F0`（读地址 -1）。
// 两个错误：① 处理函数里的 CRT 格式化；② 还钩了 `0xA1FB00`（疑似热路径）。
//
// 第二版（零 CRT、只钩 `0x2319D00`）**跑通了**：15 次命中、虚表全匹配、微信没崩。
// 但数据说明 `0x2319D00` 是在**几乎为空的对象**上被调用的（4 个指针指向二进制堆内存，
// 没有 XML、连 wxid 都没有）—— 它**不携带邀请内容**。
//
// 第三版（本版）：一次部署、**多个候选点**，把整条链路一次抓全。
//   * 处理函数**仍然零 CRT**（手写 AppHex/AppDec）；
//   * **两档**：便宜档（每个目标每次命中只写一行 ~80 字节，上限 300 次）
//     + 富档（每个目标**前 6 次**才做 hexdump + 顺指针读串）。
//     这样即使某个目标是热路径，也不会被日志压死；
//   * 每个目标的 hook 长度都**单独用反汇编确认过落在指令边界上**
//     （`0xA1B1B0` 是 11，其余是 5 或 7 —— 见 _audit/probe_hooklens.py）。
// ===================================================================
#include "hook_voip.h"
#include "Hook_Method.h"
#include "global.h"

#include <windows.h>
#include <cstdint>

namespace hook
{
namespace
{
    // ---------------------------------------------------------------- 目标表
    // 每条的 hook_len 都是反汇编确认过的**指令边界**（不要凭感觉改）。
    struct TargetDef
    {
        uintptr_t   rva;
        size_t      len;
        const char* name;
    };

    const TargetDef g_targets[] = {
        // ★ H0 是关键：`rdx` 就是消息对象 —— `+0x018`/`+0x058` = 己方 wxid，
        //   `+0x038` = 对方 wxid，`+0x180` = 邀请 XML，`+0x1c0` = <msgsource>。
        { 0x2319D00,  5, "voipmsg_layer"  },
        { 0xA208C0,   7, "payload_ctor"   },
        { 0xA1B1B0,  11, "type_dispatch"  },   // rdx==0x32 就是"发起邀请"那一刻
        { 0x2319530,  5, "serializer"     },
        { 0x2A6E220,  5, "type50_handler" },
        // ⚠️ 故意**不再钩** 0xA1FB00 / 0xA1B820：它们对所有消息都触发（上两轮
        //    各 1000+ 行噪声，占满 300KB 日志）。信息全在 H0~H4 里。
    };
    constexpr int NT = (int)(sizeof(g_targets) / sizeof(g_targets[0]));

    // 每个目标自己的虚表（用来判"这个对象是不是我们认识的那个"）。
    // 只对 0x2319D00 确定；别的目标先不自校验，直接 dump。
    constexpr uintptr_t VOIP_INVITE_VTBL_RVA = 0x82FBCB8;

    constexpr LONG   MAX_CHEAP = 3000;  // 每目标「一行」上限
    constexpr LONG   MAX_RICH  = 6;     // 每目标「完整 dump」上限
    // 富档读多少字节。**0x220 是踩过坑才定下来的**：
    //   ① 邀请对象的命名字段在 `+0x170~0x1d0`（负载很可能就填在那儿）；
    //   ② 类型50 那个对象里，2004(0x7d4) 这一档的字段在 `+0x080~0x0f0`；
    // 上一版写 0x100，正好把邀请对象的字段区整段切掉，白跑一轮。
    constexpr size_t RICH_READ = 0x220;
    // 富档缓冲。一路 12KB → 32KB → 64KB → **256KB**：
    // 现在两个对象都 dump、预览 1KB、hex 512 字节，一个富档实测能到 ~80KB，
    // 64KB 会**静默截断**（截断和"没抓到"在日志里长得一样，最费轮次）。
    constexpr size_t BUFSZ     = 262144;
    // ⚠️ 顺指针**不再设条数上限**（只受 RICH_READ 的槽位限制 = 0x220/8 = 68 个）。
    //    上一版写 12，而我把「计一条」的判据从 k>=3 放宽到 k>=1 之后，
    //    计数涨得飞快、12 条在前半段就用完 —— **负载在 +0x98 就再也没被读到**。
    //    教训：上限要卡在**输出总量**上，别卡在「命中数」上。
    constexpr size_t MAX_STR   = 1200;  // 每个指针最多看这么多字节
    constexpr size_t SHOW_ASC  = 1024;  // 可打印预览最多这么多字符
    constexpr size_t SHOW_HEX  = 512;   // 十六进制最多这么多字节
    // 为什么一路加到现在这个值：邀请 XML（`<voipinvitemsg>…</voipinvitemsg>
    // <voipextinfo>…`）比 128 字符长得多，前几轮都被**截断**在
    // `<voipextinfo><recvtim` 就没了 —— 窗口开小等于白跑一轮。

    HANDLE    g_file = INVALID_HANDLE_VALUE;
    uintptr_t g_base = 0;
    uintptr_t g_img_lo = 0;
    uintptr_t g_img_hi = 0;
    volatile LONG g_cheap[NT] = { 0 };
    volatile LONG g_rich[NT]  = { 0 };

    // ---------------------------------------------------------------- 零 CRT 小工具
    int Len(const char* s) { int n = 0; while (s && s[n]) n++; return n; }

    bool TempPathFor(const wchar_t* name, wchar_t* out, size_t cap)
    {
        wchar_t tmp[MAX_PATH] = {};
        if (GetTempPathW(MAX_PATH, tmp) == 0) return false;
        size_t i = 0;
        while (tmp[i] && i + 1 < cap) { out[i] = tmp[i]; i++; }
        size_t j = 0;
        while (name[j] && i + 1 < cap) { out[i++] = name[j++]; }
        out[i] = 0;
        return true;
    }

    bool MarkerExists(const wchar_t* name)
    {
        wchar_t path[MAX_PATH] = {};
        if (!TempPathFor(name, path, MAX_PATH)) return false;
        return GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES;
    }

    bool OpenLogFile()
    {
        wchar_t path[MAX_PATH] = {};
        if (!TempPathFor(L"voip_capture.log", path, MAX_PATH)) return false;
        g_file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, nullptr,
                             CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
        return g_file != INVALID_HANDLE_VALUE;
    }

    void WriteRaw(const char* s, int n)
    {
        if (g_file == INVALID_HANDLE_VALUE || n <= 0) return;
        DWORD wrote = 0;
        WriteFile(g_file, s, (DWORD)n, &wrote, nullptr);
    }
    void WriteStr(const char* s) { WriteRaw(s, Len(s)); }

    struct Buf
    {
        char*  p;
        size_t cap;
        size_t used;
    };

    void AppCh(Buf& b, char c) { if (b.used + 1 < b.cap) b.p[b.used++] = c; }
    void AppStr(Buf& b, const char* s) { if (!s) return; while (*s) AppCh(b, *s++); }

    void AppHex(Buf& b, uint64_t v, int minDigits)
    {
        char t[18];
        int n = 0;
        if (v == 0) t[n++] = '0';
        while (v) {
            unsigned d = (unsigned)(v & 0xF);
            t[n++] = (char)(d < 10 ? ('0' + d) : ('a' + d - 10));
            v >>= 4;
        }
        while (n < minDigits && n < 17) t[n++] = '0';
        while (n > 0) AppCh(b, t[--n]);
    }

    void AppDec(Buf& b, int64_t v)
    {
        if (v < 0) { AppCh(b, '-'); v = -v; }
        char t[24];
        int n = 0;
        if (v == 0) t[n++] = '0';
        while (v) { t[n++] = (char)('0' + (int)(v % 10)); v /= 10; }
        while (n > 0) AppCh(b, t[--n]);
    }

    void AppHexRow(Buf& b, const unsigned char* p, uint64_t rowOff, size_t n)
    {
        AppStr(b, "+0x");
        AppHex(b, rowOff, 3);
        AppStr(b, "  ");
        for (size_t i = 0; i < 16; i++) {
            if (i < n) AppHex(b, p[i], 2); else AppStr(b, "  ");
            AppCh(b, ' ');
        }
        AppCh(b, '|');
        for (size_t i = 0; i < n; i++)
            AppCh(b, (p[i] >= 0x20 && p[i] < 0x7f) ? (char)p[i] : '.');
        AppCh(b, '|');
        AppStr(b, "\r\n");
    }

    bool InImage(uintptr_t v) { return v >= g_img_lo && v < g_img_hi; }

    // ---------------------------------------------------------------- 富档
    // ⚠️ 这里不许出现任何 CRT 调用，也不许有需要析构的 C++ 对象。
    int BuildRich(int id, CALL_CONTEXT* ctx, LONG seq, char* out, size_t outsz)
    {
        Buf b = { out, outsz, 0 };

        AppStr(b, "\r\n==== rich #");
        AppDec(b, seq);
        AppStr(b, " target ");
        AppDec(b, id);
        AppStr(b, " ");
        AppStr(b, g_targets[id].name);
        AppStr(b, " ====\r\nr  rcx="); AppHex(b, (uintptr_t)ctx->rcx, 16);
        AppStr(b, " rdx="); AppHex(b, (uintptr_t)ctx->rdx, 16);
        AppStr(b, " r8=");  AppHex(b, (uintptr_t)ctx->r8, 16);
        AppStr(b, " r9=");  AppHex(b, (uintptr_t)ctx->r9, 16);
        AppStr(b, "\r\nr  rax="); AppHex(b, (uintptr_t)ctx->rax, 16);
        AppStr(b, " rbx="); AppHex(b, (uintptr_t)ctx->rbx, 16);
        AppStr(b, " rsi="); AppHex(b, (uintptr_t)ctx->rsi, 16);
        AppStr(b, " rdi="); AppHex(b, (uintptr_t)ctx->rdi, 16);
        AppStr(b, "\r\n");

        // ② **两个对象都 dump**：`rcx` 是"当前对象"，而 `rdx` 往往是**别的东西** ——
        //    对 H0(0x2319D00)/H4(0xA1B1B0) 来说 `rdx` 就是**会话对象**
        //    （大小 0x2D8，wxid 就在里面）。以前只 dump rcx，把会话里的 wxid
        //    白漏了好几轮 —— 而所有"发送"函数都只吃这个会话对象。
        const uintptr_t objs[2]   = { (uintptr_t)ctx->rcx, (uintptr_t)ctx->rdx };
        const char*     labels[2] = { "rcx", "rdx" };

        for (int oi = 0; oi < 2; ++oi)
        {
            const uintptr_t obj = objs[oi];

            AppStr(b, "\r\n---- obj ");
            AppStr(b, labels[oi]);
            AppStr(b, " = ");
            AppHex(b, obj, 16);
            AppStr(b, " ----\r\n");

            if (obj < 0x10000) { AppStr(b, "not a plausible pointer\r\n"); continue; }

            __try
            {
                uintptr_t vt = *(uintptr_t*)obj;
                AppStr(b, "vtable="); AppHex(b, vt, 16);
                if (vt == g_base + VOIP_INVITE_VTBL_RVA) AppStr(b, "  MATCH(invite)\r\n");
                else AppStr(b, "\r\n");
                for (size_t row = 0; row < RICH_READ; row += 16)
                    AppHexRow(b, (const unsigned char*)(obj + row), row, 16);
            }
            __except (EXCEPTION_EXECUTE_HANDLER)
            {
                AppStr(b, "<read fault>\r\n");
            }

            AppStr(b, "-- followed strings --\r\n");
            // ⚠️ 这里**故意没有「最多跟几个指针」的上限**：上限只由 RICH_READ 的槽位决定。
            //    上一版卡了 12 条，结果负载所在的 +0x98 永远轮不到 —— 见 MAX_STR 上面的说明。
            for (size_t o = 0; o + 8 <= RICH_READ; o += 8)
            {
                uintptr_t v = 0;
                __try { v = *(uintptr_t*)(obj + o); }
                __except (EXCEPTION_EXECUTE_HANDLER) { break; }
                if (v < 0x10000 || v > 0x7ffffffeffffULL) continue;

                AppStr(b, "+");
                AppHex(b, o, 3);
                AppStr(b, " -> ");
                AppHex(b, v, 16);
                __try
                {
                    const unsigned char* q = (const unsigned char*)v;
                    size_t k = 0;
                    while (k < MAX_STR && q[k] != 0) k++;
                    if (k >= 1)
                    {
                        AppStr(b, "  \"");
                        for (size_t i = 0; i < k && i < SHOW_ASC; i++)
                            AppCh(b, (q[i] >= 0x20 && q[i] < 0x7f) ? (char)q[i] : '.');
                        AppCh(b, '"');
                        // 再给一份**十六进制**。为什么必须有：
                        // 负载里那些"分隔符"是不可打印的**非零**字节（很可能是长度前缀），
                        // 只靠上面用 '.' 顶替的可打印预览根本看不出它们的真实值。
                        AppStr(b, " hex=");
                        for (size_t i = 0; i < k && i < SHOW_HEX; i++) AppHex(b, q[i], 2);
                    }
                }
                __except (EXCEPTION_EXECUTE_HANDLER) { AppStr(b, " <fault>"); }
                AppStr(b, "\r\n");
            }
        }
        return (int)b.used;
    }

    // ---------------------------------------------------------------- 统一入口
    void OnHit(int id, CALL_CONTEXT* ctx)
    {
        if (g_file == INVALID_HANDLE_VALUE) return;

        // ---- 便宜档：每个目标每次命中只写一行（手写、不读内存）
        LONG c = InterlockedIncrement(&g_cheap[id]);
        if (c > MAX_CHEAP) return;

        char line[192];
        Buf b = { line, sizeof(line), 0 };
        AppStr(b, "H");
        AppDec(b, id);
        AppStr(b, " ");
        AppStr(b, g_targets[id].name);
        AppStr(b, " rcx="); AppHex(b, (uintptr_t)ctx->rcx, 16);
        AppStr(b, " rdx="); AppHex(b, (uintptr_t)ctx->rdx, 16);
        AppStr(b, " r8=");  AppHex(b, (uintptr_t)ctx->r8, 16);
        AppStr(b, " r9=");  AppHex(b, (uintptr_t)ctx->r9, 16);
        AppStr(b, "\r\n");
        WriteRaw(line, (int)b.used);

        // ---- 富档：只做前几次
        LONG r = InterlockedIncrement(&g_rich[id]);
        // 前 MAX_RICH 次全做；之后**每 100 次再采样一次**。
        // 为什么：万一某个目标是热路径，前几次可能全在启动期，
        // 只有采样才能保证"真正打电话那一段"也有完整 dump。
        if (r > MAX_RICH && (r % 100) != 0) return;

        char* buf = (char*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, BUFSZ);
        if (!buf) return;
        int len = BuildRich(id, ctx, r, buf, BUFSZ);
        if (len > 0) WriteRaw(buf, len);
        HeapFree(GetProcessHeap(), 0, buf);
    }

    // `Hook_Inline` 每个钩子只收一个 handler，所以要一目标一个薄壳。
    void H0(CALL_CONTEXT* c) { OnHit(0, c); }
    void H1(CALL_CONTEXT* c) { OnHit(1, c); }
    void H2(CALL_CONTEXT* c) { OnHit(2, c); }
    void H3(CALL_CONTEXT* c) { OnHit(3, c); }
    void H4(CALL_CONTEXT* c) { OnHit(4, c); }
    CallHookHandler g_handlers[NT] = { &H0, &H1, &H2, &H3, &H4 };
}   // namespace

bool InstallVoipCapture()
{
    if (!MarkerExists(L"wx_voip_capture.on"))
        return false;

    g_base = (uintptr_t)g_hWeixinDll;
    if (!g_base) { WriteStr("[voip] no Weixin base\r\n"); return false; }

    __try
    {
        IMAGE_DOS_HEADER* dos = (IMAGE_DOS_HEADER*)g_base;
        IMAGE_NT_HEADERS64* nt = (IMAGE_NT_HEADERS64*)(g_base + dos->e_lfanew);
        g_img_lo = g_base;
        g_img_hi = g_base + nt->OptionalHeader.SizeOfImage;
    }
    __except (EXCEPTION_EXECUTE_HANDLER)
    {
        g_img_lo = g_base;
        g_img_hi = g_base + 0x10000000;
    }

    if (!OpenLogFile()) return false;

    WriteStr("voip capture  multi-target  strings=on\r\n");
    WriteStr("targets:\r\n");
    for (int i = 0; i < NT; ++i)
    {
        WriteStr("  H");
        char n[8]; int k = 0; int v = i;
        if (!v) n[k++] = '0';
        while (v) { n[k++] = (char)('0' + v % 10); v /= 10; }
        char rev[8]; for (int j = 0; j < k; j++) rev[j] = n[k - 1 - j];
        WriteRaw(rev, k);
        WriteStr(" ");
        WriteStr(g_targets[i].name);
        WriteStr("\r\n");
    }
    WriteStr("max cheap=300/target, rich=6/target\r\n\r\n");

    int ok = 0;
    for (int i = 0; i < NT; ++i)
    {
        void* addr = (void*)(g_base + g_targets[i].rva);
        bool r = Hook_Inline(addr, g_targets[i].len, g_handlers[i]);
        WriteStr(r ? "  install OK   " : "  install FAIL ");
        WriteStr(g_targets[i].name);
        WriteStr("\r\n");
        if (r) ++ok;
    }

    WriteStr("\r\n");
    if (ok == 0)
    {
        WriteStr("no hook installed\r\n");
        CloseHandle(g_file);
        g_file = INVALID_HANDLE_VALUE;
    }
    return ok > 0;
}
}   // namespace hook
