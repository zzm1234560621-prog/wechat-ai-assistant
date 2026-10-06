#include <cstdint>
#include <cstdio>
#include <string>
#include <Windows.h>
#include <winternl.h>
#include <cstring>
#include <sstream>
#include <atomic>
#include <cctype>
#include <cwctype>
#include <vector>

#include "http_server.h"
#include "Hook_Method.h"

#include "global.h"
#include <MinHook.h>
#include "tools.h"
#include "HookManager.h"
#include "json.hpp"
#include "wx_ini_reader.h"
#include "inline_weixin_dll_load.h"
#include "hook_xlog.h"
#include "hook_voip.h"

// ⚠️ 这里**不能** include "db_mgr.h"：它会把 xdb/sqlite3.h 拖进来，而那个头
// 依赖调用方先提供 sqlite3 的前置定义，在本文件里编不过（实测 C2061 一片）。
// 所以句柄表相关的两件事都在 global.h 里以**自由函数**声明，实现在 db_mgr.cpp：
//   bool DbLayerReady();          —— db_mgr 自己的门禁（唯一读点）
//   bool DbHandleTableAlive();    —— 给就绪判据/看门狗用的存活校验

#include <map>
#include <set>

using json = nlohmann::json;

typedef NTSTATUS(NTAPI* PFN_NtQueryInformationProcess)(
    HANDLE,
    PROCESSINFOCLASS,
    PVOID,
    ULONG,
    PULONG
    );



DWORD GetParentProcessId()
{
    PFN_NtQueryInformationProcess NtQueryInformationProcess =
        (PFN_NtQueryInformationProcess)GetProcAddress(
            GetModuleHandleW(L"ntdll.dll"),
            "NtQueryInformationProcess"
        );

    if (!NtQueryInformationProcess)
        return 0;

    PROCESS_BASIC_INFORMATION pbi = { 0 };

    NTSTATUS status = NtQueryInformationProcess(
        GetCurrentProcess(),
        ProcessBasicInformation,
        &pbi,
        sizeof(pbi),
        nullptr
    );

    if (status != 0)
        return 0;

    return (DWORD)(ULONG_PTR)pbi.Reserved3;
}


DWORD WINAPI AfterLoginInitThread(LPVOID)
{
    // 等待登录成功
    while (g_IsLogin != 1)
    {
        Sleep(300);
    }

    return 0;
}

// 过低版本
void Patch_Low_Version()
{
    // XWECHAT_MAIN_CLAZZ_OFFSET 4.1.8.67
    DWORD_PTR baseAddress = (DWORD_PTR)g_hWeixinDll + reinterpret_cast<uintptr_t>(XWECHAT_MAIN_CLAZZ_OFFSET);

    DWORD_PTR* pPointer = (DWORD_PTR*)baseAddress;
    if (*pPointer == NULL) {
        // 处理空指针情况
        return;
    }

    DWORD_PTR targetAddress = (*pPointer) + 0xB8 + 0x90;        //80  = 00000000F2541843



    // 直接通过指针修改
    BYTE* pTarget = (BYTE*)targetAddress;

    // 修改内存保护属性
    DWORD oldProtect;
    VirtualProtect(pTarget, 4, PAGE_EXECUTE_READWRITE, &oldProtect);

    *(pTarget) = 0x43;      // 低位字节 = 67
    *(pTarget + 1) = 0x18;
    *(pTarget + 2) = 0x54;  // 高位字节
    *(pTarget + 3) = 0xF2;  

    // 恢复保护属性
    VirtualProtect(pTarget, 4, oldProtect, &oldProtect);
}


void Patch_Low_Version_m2()
{
    // 4.1.8.67 addresses
    struct PatchInfo {
        DWORD_PTR addr;
        BYTE bytes[4];
    };

    // F2510201
    // F2541843
    PatchInfo patches[] = {
        { (DWORD_PTR)g_hWeixinExe + 0x36E2, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x18EB, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x1B0E, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x204B, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0xDE4C33, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x2AC9481, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x3248CE9, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x379D66E, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinExe + 0x204FA0, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0xDE540C, { 0x43, 0x19, 0x6C, 0xF2 } }, 
        { (DWORD_PTR)g_hWeixinDll + 0xA3518D0, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0xA3518D4, { 0x43, 0x19, 0x6C, 0xF2 } }
    };

    DWORD oldProtect;
    for (int i = 0; i < 3; i++) {
        VirtualProtect((LPVOID)patches[i].addr, 4, PAGE_EXECUTE_READWRITE, &oldProtect);
        memcpy((void*)patches[i].addr, patches[i].bytes, 4);
        VirtualProtect((LPVOID)patches[i].addr, 4, oldProtect, &oldProtect);
    }

}


//启用防撤回
void Patch_Revoke()
{
    // 计算目标地址
    DWORD_PTR targetAddress = (DWORD_PTR)g_hWeixinDll + g_Patch_Revoke;

    // 直接通过指针修改
    BYTE* pTarget = (BYTE*)targetAddress;

    // 修改内存保护属性
    DWORD oldProtect;
    VirtualProtect(pTarget, 2, PAGE_EXECUTE_READWRITE, &oldProtect);

    *pTarget = 0x90;           // 第一个字节改为 nop
    *(pTarget + 1) = 0xE9;     // 第二个字节改为 jmp 操作码

    // 恢复保护属性
    VirtualProtect(pTarget, 2, oldProtect, &oldProtect);
}


// ===== 本地补丁：等「登录就绪」再放行（2026-10-05 收紧） =====
//
// 原版靠一个「登录检测 hook」把 g_IsLogin 置 1，那段代码在开源快照里被移除了
// （README: main 分支已移除 ...Hook 安装与处理代码），所以 g_IsLogin 恒为 0，
// db_mgr.cpp 里 if (!g_IsLogin) 的门禁永远成立，QueryDB 永远返回空。
//
// 但不能在 Weixin.dll 一加载就无条件置 1：那时账号还没登录、db_storage 里的
// 库句柄还没建立，QueryDB 会拿垃圾句柄去调 sqlite，把微信搞崩——实测三次崩溃：
//   0xC0000005 读 0x10000              @ Weixin.dll+0x12BB489
//   0xC0000005 读 0xFFFFFFFFFFFFFFFF   @ Weixin.dll+0x12BB50C
//   0xC0000374 堆损坏
//
// ---- 第一版判据（2026-10-03）：只看「有 .db 被写过」——**不够** ----
// 判据是「dll 加载时刻之后，任意 .db 的 mtime ≥ 加载时刻」。实测（2026-10-05 22:24）
// 它会在**微信自己还在起 DB 层的时候**就成立：微信启动时本来就在写 db（-wal/-shm 一堆），
// 于是闸门早早打开，bot 5 秒一轮的查询正好压在句柄表/索引尚未建完的窗口上。
// 后果就是 10-05 那一晚 22:24、22:27 连着两次 `Weixin.dll+0x32BB489 读 0x1`
// （和上面那三次同一个函数族：db 层对象被当成有效对象用 → 池链表指针被写坏）。
//
// ---- 现判据（三重证据，全部满足才放行） ----
//   ① **文件侧连续稳定**：三段文件恒等组（session / message / contact）里，至少 2 组的
//      **最新 mtime 连续 3 次探测都严格上升**（探测 5 秒一步 ⇒ 至少 10~15 秒持续在写）。
//      只认「一直变新」，不认「比某个时刻新」——后者正是旧判据被启动写库骗过的原因。
//   ② **句柄真能查**：searchDatabases() 能枚举出 sqlite 句柄，且三类核心库
//      （session / message / contact）里至少 2 类拿得到句柄。只验「文件在写」不验句柄，
//      就是这次崩溃的根因。
//   ③ **不抖**：上面两条都满足后再缓 1.5 秒复核一次句柄表，仍存活才放行。
//
// 放行之后由看门狗继续看着（fix「只置 1、从不置回 0」）：
//   每 5 秒 `HandleTableAlive()` 一次；句柄表整个失效（掉登录 / 微信自己重建句柄表）时，
//   立刻把 g_DbLayerReady 与 g_IsLogin 一起置回 0 并记一条 LoginGate 说明；之后
//   **等「文件在写 + 句柄能查」再次成立**才重新放行（重新扫码登录就自动恢复，
//   不用重启微信、不用重启助手）。这样 /QueryDB/status 上的 IsLogin 才是真话。
//
// ⚠️ 这里刻意**不做**的事：
//   * 不调用 getDatabaseInfo()（那是 force_rescan 的重活：getDatabaseInfo 先 m_dbs.clear()
//     再全内存扫描，调勤了会把 700MB 的微信拖死，见 CLAUDE.md 的 hook 铁律）；
//   * 不复用 g_Patch_Revoke 那类写内存的补丁，不动微信任何内部状态；
//   * 句柄校验只「读一读地址能不能读」（DatabaseMgr::HandleTableAlive），不碰 sqlite 状态机。

static ULARGE_INTEGER g_loginLoadTime{};   // 当前这轮「就绪」判定开始计时的时刻
static volatile LONG  g_readyThreadStarted = 0;

// 文件恒等组：三类核心库。**至少 2 组**要同时满足「连续在写 + 拿得到句柄」。
// 宽松一档（不是「3 组全中」）是故意的：新账号 / 刚迁移数据时可能先出现一部分库，
// 要求全中会让闸门**永远不开**（比崩溃更难查的静默失效）。而只要求第 ① 条
// （文件在写）已经是被证伪的旧判据，所以取「2 组」这个下限。
struct CoreGroup {
    const wchar_t* name;
    std::vector<std::wstring> kws;
};
static const CoreGroup g_coreGroups[] = {
    { L"session", { L"session.db" } },
    { L"message", { L"message_0.db", L"message_fts" } },
    { L"contact", { L"contact.db", L"contact_fts" } },
};
static const int g_coreGroupNeed = 2;      // 至少几组同时成立才放行
static const int g_stableNeed = 3;         // 文件侧连续几次看到 mtime 上升
static const int g_watchIntervalMs = 5000; // 探测/看门狗周期（一次循环一步）
// ★ 两道计时闸门（2026-10-05 第三次修，前两次都被实测否掉）：
//
// 第一版：只看「有 .db 被写过」→ 被微信启动时的写库抖动骗过，在句柄表没建完时放行。
// 第二版：等「所有库 30 秒不被写」→ **实测根本不可能成立**：本机数据证明活跃账号
//         平时就是每 1~5 秒写一次库（连续采样 6 次，最新写入始终在 1~5 秒前），
//         所以闸门永远不开（IsLogin 恒 0、机器人一直不工作）。
//
// 现在：**不再拿"安静"当锚点**（那件事在活跃账号上不存在），而是：
//   ① g_sinceFirstWriteMs：从「第一次看到核心库被写」起至少等这么久 —— 压过登录后
//      那串「逐个打开库」的初始化窗口（实测 22:54:38~22:54:55，约 15~20 秒）；
//   ② 句柄表扫描（全内存，**每个进程只做一次**）：库真打开完才会枚举出句柄，
//      这是结构性证据，不会被"在写"这种时间性证据骗到；
//   ③ 之后只做**只读**存活校验（DbHandleTableAlive，不扫描），失效才关闸门。
static const int g_sinceFirstWriteMs = 25000;  // 首次见写之后至少等 25 秒（盖住初始化窗口）
static const int g_watchdogEvery = 6;          // 放行后每 6 个周期（≈30 秒）做一次存活校验

// ---- 编码 / 小文件读取的小工具（保存位置判据与诊断文案都要用）----
static std::string Utf8FromWide(const std::wstring& w)
{
    if (w.empty())
        return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(),
                                nullptr, 0, nullptr, nullptr);
    if (n <= 0)
        return std::string();
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), &out[0], n, nullptr, nullptr);
    return out;
}

static std::wstring WideFromUtf8(const std::string& s)
{
    if (s.empty())
        return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    if (n <= 0)
        return std::wstring();
    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &out[0], n);
    return out;
}

static std::string ReadSmallText(const std::wstring& path, DWORD cap = 4096)
{
    std::string out;
    HANDLE h = CreateFileW(path.c_str(), GENERIC_READ,
                           FILE_SHARE_READ | FILE_SHARE_WRITE,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE)
        return out;
    char buf[4096];
    DWORD got = 0;
    if (ReadFile(h, buf, cap < sizeof(buf) ? cap : sizeof(buf), &got, nullptr) && got > 0)
        out.assign(buf, got);
    CloseHandle(h);
    return out;
}

static std::string TrimAscii(const std::string& s)
{
    size_t a = 0, b = s.size();
    while (a < b && (unsigned char)s[a] <= ' ')
        a++;
    while (b > a && (unsigned char)s[b - 1] <= ' ')
        b--;
    return s.substr(a, b - a);
}

static bool NameInGroup(const std::string& name, const CoreGroup& g)
{
    for (const std::wstring& kw : g.kws) {
        const std::string k = Utf8FromWide(kw);
        if (!k.empty() && name.find(k) != std::string::npos)
            return true;
    }
    return false;
}

// 微信自己记的保存位置（xwechat_files 的**父目录**）。
//
// ⚠️ 和 Python 侧 `image_cache._wechat_save_roots()` 是**同一个判据，两处必须一致**：
// 微信 4.x 把「文件保存位置」记在 %APPDATA%\Tencent\xwechat\config\<哈希>.ini，
// 文件内容就是**一行路径**（本机实测：`D:\wechat`）。
//
// 为什么必须有这条（2026-10-03 真机踩到）：以前这里把路径**写死**成
// `%USERPROFILE%\Documents\xwechat_files`。用户 2026-10-02 22:25 把微信数据搬到
// `D:\wechat` 之后，那条路径**不存在**了 → 就绪判据永远不成立 →
// `g_IsLogin` 恒为 0 → `db_mgr` 的门禁永远关着 → 所有 QueryDB 一律回
// 「get database handle which named xxx.db failed」。**不报错、不打日志**，
// 正是这个项目最怕的那种静默失效。Python 侧当年踩的是同一个坑、那边已经修了，
// 这边没同步 —— 这是**重复 owner** 的代价。
static std::vector<std::wstring> WechatSaveRoots()
{
    std::vector<std::wstring> out;
    wchar_t appdata[MAX_PATH] = {};
    if (!GetEnvironmentVariableW(L"APPDATA", appdata, MAX_PATH))
        return out;

    const std::wstring dir = std::wstring(appdata) + L"\\Tencent\\xwechat\\config";
    WIN32_FIND_DATAW fd = {};
    HANDLE h = FindFirstFileW((dir + L"\\*.ini").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE)
        return out;

    do {
        if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)
            continue;
        std::string txt = TrimAscii(ReadSmallText(dir + L"\\" + fd.cFileName));
        // 只认「一行、像盘符/UNC 路径」的内容 —— 那个目录下还有别的配置 ini
        if (txt.empty() || txt.size() > 260 || txt.find('\n') != std::string::npos)
            continue;
        const bool drive = txt.size() >= 3 && isalpha((unsigned char)txt[0]) &&
                           txt[1] == ':' && (txt[2] == '\\' || txt[2] == '/');
        const bool unc = txt.size() >= 2 && txt[0] == '\\' && txt[1] == '\\';
        if (!drive && !unc)
            continue;
        std::wstring w = WideFromUtf8(txt);
        if (w.empty())
            continue;
        if (GetFileAttributesW((w + L"\\xwechat_files").c_str()) != INVALID_FILE_ATTRIBUTES)
            out.push_back(w);
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return out;
}

static std::vector<std::wstring> CandidateRoots()
{
    std::vector<std::wstring> roots;
    for (const std::wstring& r : WechatSaveRoots())
        roots.push_back(r + L"\\xwechat_files");
    wchar_t profile[MAX_PATH] = {};
    if (GetEnvironmentVariableW(L"USERPROFILE", profile, MAX_PATH))
        roots.push_back(std::wstring(profile) + L"\\Documents\\xwechat_files");
    return roots;
}

// 登录就绪判据的**可读诊断**，发布给 /QueryDB/status。
// 写侧只有就绪判据/看门狗那一条线程；开关只允许 0→1 一次（之后只在需要时覆盖）。
// 读侧拿原子快照 —— 不用锁。
static std::string g_gateNoteBuf;
static std::atomic<const char*> g_gateNotePtr{ "" };

void SetLoginGateNote(const std::string& s)
{
    g_gateNoteBuf = s;
    g_gateNotePtr.store(g_gateNoteBuf.c_str());
}

const char* LoginGateNote()
{
    return g_gateNotePtr.load();
}

// ---- 判据的可观测状态（声明在 global.h，QueryDB.cpp 透给 /QueryDB/status）----
static LoginGateInfo g_gateInfo{};      // 只由判据线程写；字段都是 POD，读侧容忍撕裂

LoginGateInfo GetLoginGateInfo() { return g_gateInfo; }

// 三段文件恒等组的「已见最大 mtime」——只增不减，判据是「比上次更新」。
// 下面这三个状态 + g_loginLoadTime 只有 LoginReadyThread 一条线程读写，
// 不需要锁；对外只经 LoginGateNote() 的原子指针发布（见 SetLoginGateNote）。
// 注意：这里记的是**所有** .db（不只是核心三组）——「还在逐个打开库」这件事本身就说明
// db 层没稳定，所以任何一个库被写都要把静默计时清零。
static std::map<std::string, ULONGLONG> g_lastMaxWrite;
static int g_fileStableChecks = 0;      // 连续几次探测看到核心库 mtime 上升
static int g_waitChecks = 0;            // 已经等了多少个探测周期（用于写诊断文案）
static bool g_anyFreshSeen = false;     // 这次进程里见过核心库被写（= 账号确实登录了）
static ULONGLONG g_lastWriteTick = 0;   // 最近一次「有库被写」的时刻（GetTickCount64 基准）
static ULONGLONG g_firstFreshTick = 0;  // 第一次看到核心库被写的时刻（≈ 本次登录的时刻）
static bool g_handleScanDone = false;   // 句柄表扫描**每个进程只做一次**（见 g_sinceFirstWriteMs 注释）

static void ResetFileClock()
{
    g_lastMaxWrite.clear();
    g_fileStableChecks = 0;
    g_anyFreshSeen = false;
    g_lastWriteTick = 0;
    g_firstFreshTick = 0;
    g_handleScanDone = false;
}

// 注意：FindFirstFileW **不支持路径中间的 `*`**（会直接返回 ERROR_INVALID_NAME=123），
// 所以只能一层层往下枚举，通配符永远放在最后一段。
static bool IsDotDir(const wchar_t* name)
{
    return name[0] == L'.' &&
           (name[1] == 0 || (name[1] == L'.' && name[2] == 0));
}

// 把「当前见到的最大 mtime」记进 g_lastMaxWrite（只增），返回这一轮有几组核心库在写。
// 同时把**每一个** .db 都记进表里：只要有任何一个库的 mtime 前进，就把静默计时清零
// （「还在逐个打开库」= 没稳定，见 g_quietNeedMs 的注释）。
static int ObserveFiles()
{
    std::set<std::string> seen;              // 这一轮看到 mtime 前进的库名
    std::set<std::string> seenCore;          // 其中属于核心三组的
    for (const std::wstring& root : CandidateRoots()) {
        if (GetFileAttributesW(root.c_str()) == INVALID_FILE_ATTRIBUTES)
            continue;

        WIN32_FIND_DATAW fd = {};
        HANDLE h = FindFirstFileW((root + L"\\*").c_str(), &fd);
        if (h == INVALID_HANDLE_VALUE)
            continue;
        do {
            if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || IsDotDir(fd.cFileName))
                continue;
            const std::wstring storage = root + L"\\" + fd.cFileName + L"\\db_storage";
            WIN32_FIND_DATAW sd = {};
            HANDLE sh = FindFirstFileW((storage + L"\\*").c_str(), &sd);
            if (sh == INVALID_HANDLE_VALUE)
                continue;
            do {
                if (!(sd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || IsDotDir(sd.cFileName))
                    continue;
                const std::wstring sub = storage + L"\\" + sd.cFileName;
                WIN32_FIND_DATAW qd = {};
                HANDLE qh = FindFirstFileW((sub + L"\\*.db").c_str(), &qd);
                if (qh == INVALID_HANDLE_VALUE)
                    continue;
                do {
                    std::wstring lower = qd.cFileName;
                    for (wchar_t& c : lower)
                        c = (wchar_t)towlower(c);
                    const std::string base = Utf8FromWide(lower);
                    ULARGE_INTEGER w{};
                    w.LowPart  = qd.ftLastWriteTime.dwLowDateTime;
                    w.HighPart = qd.ftLastWriteTime.dwHighDateTime;
                    if (w.QuadPart == 0)
                        continue;
                    auto it = g_lastMaxWrite.find(base);
                    if (it == g_lastMaxWrite.end() || w.QuadPart > it->second) {
                        g_lastMaxWrite[base] = w.QuadPart;
                        seen.insert(base);
                        for (const CoreGroup& g : g_coreGroups) {
                            if (NameInGroup(base, g)) { seenCore.insert(base); break; }
                        }
                    }
                } while (FindNextFileW(qh, &qd));
                FindClose(qh);
            } while (FindNextFileW(sh, &sd));
            FindClose(sh);
        } while (FindNextFileW(h, &fd));
        FindClose(h);
    }

    // 静默计时：这一轮有任何库被写就把计时推到"现在"
    const ULONGLONG now = GetTickCount64();
    if (!seen.empty()) {
        g_lastWriteTick = now;
        if (!seenCore.empty()) {
            if (g_firstFreshTick == 0)
                g_firstFreshTick = now;    // 记下「这次登录」的起点
            g_anyFreshSeen = true;
        }
    }

    int groups = 0;
    for (const CoreGroup& g : g_coreGroups) {
        for (const std::string& nm : seenCore) {
            if (NameInGroup(nm, g)) { groups++; break; }
        }
    }
    // 诊断用：把「首次见写之后过了多久」和「离下限还差多久」都算出来
    g_gateInfo.quietMs = (g_firstFreshTick == 0) ? 0 : (uint32_t)(now - g_firstFreshTick);
    g_gateInfo.dbsTracked = (uint32_t)g_lastMaxWrite.size();
    return groups;
}

// 句柄侧：**只读缓存**数「核心组里有几组真的拿得到句柄」。
// ★ 为什么不再调 DbForceRescan（2026-10-05 第二次崩后改）：
//   强制重扫 = 在未就绪窗口里主动去枚举/戳进程内的 sqlite 句柄，那是**新增的探测面**；
//   实测那版上线 2 分钟就又崩了一次（Weixin.dll+0x32BB97F，同一个池函数家族）。
//   现在判据**完全不碰句柄表**：句柄由 db_mgr 在第一次真实查询时自己建立（老代价），
//   这里的快照只用于诊断与"表是否为空"这一条粗判据 —— 只读、不触发任何扫描。
static int ScanCoreGroupsWithHandles(int& totalHandles)
{
    std::vector<std::pair<std::string, uint64_t>> dbs;
    DbHandlesSnapshot(dbs);
    totalHandles = (int)dbs.size();
    int groups = 0;
    for (const CoreGroup& g : g_coreGroups) {
        for (auto& it : dbs) {
            if (NameInGroup(it.first, g)) { groups++; break; }
        }
    }
    return groups;
}

static void SetGateState(const char* note, uint64_t isLogin, uint64_t dbReady)
{
    g_IsLogin = isLogin;
    g_DbLayerReady = dbReady;
    SetLoginGateNote(note);
}

// 就绪判据 + 看门狗线程（合成一条：它本来就只有这一件事要干）。
// 状态只有这条线程碰（LoginGateNote 用原子发布给读侧），所以不需要锁。
static DWORD WINAPI LoginReadyThread(LPVOID)
{
    g_gateInfo.running = 1;
    g_gateInfo.handleGroups = 0xFFFFFFFFu;   // 「还没扫过句柄」
    // 允许句柄表扫描（**不是**放行查询）。必须在第一次探测之前打开，否则判据自己被门禁挡住。
    // 反过来，查询门禁（g_DbLayerReady）仍保持关着，直到三重证据全中。
    g_HandleScanAllowed = 1;

    for (;;) {
        Sleep(g_watchIntervalMs);
        g_gateInfo.cycles++;

        if (g_DbLayerReady != 0) {
            // ---- 放行之后的看门狗（**低频**：约 30 秒一次，别每 5 秒去碰句柄表）----
            if ((g_gateInfo.cycles % (uint64_t)g_watchdogEvery) != 0)
                continue;
            // 存活判定走 db_mgr（只有它碰 sqlite 的私有类型）；快照只为记个数量供诊断。
            // ⚠️ 「缓存是空的」**不等于**「db 层坏了」：句柄表是由查询路径建立的，
            //    我们放行之后才可能出现第一个查询。所以只有「缓存里有句柄、但一个都不能读」
            //    才关闸门；空缓存直接跳过（否则会在还没开始查的时候就自己把闸门关上）。
            std::vector<std::pair<std::string, uint64_t>> snap;
            DbHandlesSnapshot(snap);
            if (snap.empty()) {
                continue;
            }
            const bool tableAlive = DbHandleTableAlive();
            if (tableAlive) {
                g_gateInfo.handlesAlive = snap.size();
                continue;
            }

            // 只关闸门 + 重设计时起点；**不清缓存、不重扫**（缓存由 db_mgr 自己的
            // searchDatabases 在下次需要时决定要不要重建）。
            FILETIME ft{};
            GetSystemTimeAsFileTime(&ft);
            g_loginLoadTime.LowPart  = ft.dwLowDateTime;
            g_loginLoadTime.HighPart = ft.dwHighDateTime;
            ResetFileClock();
            g_waitChecks = 0;
            g_gateInfo.dbReady = 0;
            g_gateInfo.handlesAlive = 0;
            SetGateState("db 层句柄表已失效（掉登录或微信重建了句柄表），已关闸门等就绪", 0, 0);
            continue;
        }

        // ---- 未放行：先看文件侧是否「连续在写」 ----
        const int groups = ObserveFiles();
        g_gateInfo.groupsFresh = (uint32_t)groups;

        // 边界：候选根一个都不存在 = **配置问题**（数据被搬到别处了），不是「还没登录」。
        // 如实写进 /QueryDB/status，别让用户对着一个恒为 0 的 IsLogin 猜。
        bool anyRoot = false;
        for (const std::wstring& r : CandidateRoots()) {
            if (GetFileAttributesW(r.c_str()) != INVALID_FILE_ATTRIBUTES) { anyRoot = true; break; }
        }
        if (!anyRoot) {
            std::string note = "登录就绪判据失败：候选数据根一个都不存在 -> ";
            const std::vector<std::wstring> roots = CandidateRoots();
            for (size_t i = 0; i < roots.size(); i++) {
                if (i) note += " | ";
                note += Utf8FromWide(roots[i]);
            }
            SetLoginGateNote(note);
            continue;
        }

        g_waitChecks++;
        if (groups >= g_coreGroupNeed) {
            g_fileStableChecks++;
        }
        else {
            // 一旦「在写的核心组不够」就清零：**连续**才算稳定，否则微信启动时的
            // 一阵写库抖动就能骗过判据（旧判据正是这么被绕过的）。
            g_fileStableChecks = 0;
        }
        g_gateInfo.stableChecks = (uint32_t)g_fileStableChecks;
        g_gateInfo.waitChecks = (uint32_t)g_waitChecks;

        // 每一轮都把真实计数写进 LoginGate（只在变化时写，避免每 5 秒刷一次字符串）。
        // 为什么必须写：闸门没开时，用户/上层只能看到 IsLogin=0；不摆出计数就分不清
        // 「静默没达标」「句柄表是空的」「线程没跑」——那正是静默失效。
        {
            static std::string lastGeneric;
            char buf[360];
            snprintf(buf, sizeof(buf),
                     "gate[run=%u cyc=%llu files=%d/%d after=%ums/%ums dbs=%u handles=%d/%d total=%u]",
                     (unsigned)g_gateInfo.running, (unsigned long long)g_gateInfo.cycles,
                     groups, g_coreGroupNeed,
                     (unsigned)g_gateInfo.quietMs, (unsigned)g_sinceFirstWriteMs,
                     (unsigned)g_gateInfo.dbsTracked,
                     (g_gateInfo.handleGroups == 0xFFFFFFFFu) ? -1 : (int)g_gateInfo.handleGroups,
                     g_coreGroupNeed, (unsigned)g_gateInfo.handleTotal);
            if (lastGeneric != buf) {
                lastGeneric = buf;
                SetLoginGateNote(buf);
            }
        }

        // ★ 判据（第四次改，也是最保守的一版）：
        //   ① 这次进程真的登录了（见过核心库被写）；
        //   ② 距「第一次看到被写」至少 25 秒 —— 压过登录后逐个打开库的窗口；
        //   ③ **到此就放行**。句柄表交给查询路径自己去建（老代价，见下方注释）。
        // 为什么不再在 hook 里主动扫句柄表（前三版都死在这上面）：
        //   * 扫描要遍历进程内存里的候选指针、还读每个候选的 sqlite 头 —— 微信正在
        //     建/重建连接表时做这件事，实测会崩（22:54、23:09 两次崩溃都紧贴一次登录，
        //     而那两版都有"登录窗口内主动扫"的动作）；
        //   * 句柄表**不是必须由我们扫**：db_mgr.searchDatabases 在第一次真实查询时
        //     会自己扫一遍并建好缓存（这是 hook 原本就有的行为，跑了几个月）。
        //   * 于是判据只保留"时间 + 文件"这两件不碰微信内部的事。
        if (!g_anyFreshSeen)
            continue;                     // 还没登录：不写库，也没得查
        if (g_firstFreshTick == 0 ||
            GetTickCount64() - g_firstFreshTick < (ULONGLONG)g_sinceFirstWriteMs)
            continue;                     // 还在初始化窗口里：稳住，别催

        if (!g_handleScanDone) {
            // 只做**只读**快照（此时缓存通常为空，那是正常的：句柄由查询路径建立）。
            // ⚠️ 这里**不调用任何会扫描的东西** —— ScanCoreGroupsWithHandles 现在只读快照。
            int total0 = 0;
            const int groups0 = ScanCoreGroupsWithHandles(total0);
            g_gateInfo.handleTotal = (uint32_t)total0;
            g_gateInfo.handleGroups = (uint32_t)groups0;
            std::vector<std::pair<std::string, uint64_t>> snap;
            DbHandlesSnapshot(snap);
            std::string names;
            for (size_t i = 0; i < snap.size() && i < 24; i++) {
                if (i) names += ",";
                names += snap[i].first;
            }
            if (names.size() > 300) names.resize(300);
            g_gateInfo.handleNames = names;
            g_handleScanDone = true;
        }

        // ★ 到此放行：不再做任何"存活校验"（那会读句柄表）。句柄表由查询路径自己建、
        //   自己修（db_mgr 的坏句柄重建 + live_history 的 force_rescan 都在那条路上）。
        //   看门狗仍在（低频）：读到表不可读就把 IsLogin 置回 0；但如果句柄表压根还没被
        //   查询路径建立过（缓存空），它**不关闸门** —— 空缓存不等于"db 层坏了"。
        char buf[512];
        snprintf(buf, sizeof(buf),
                 "ok：首次见核心库被写后已过 %u 秒（压过初始化窗口，共记录 %u 个库）；"
                 "句柄表交给查询路径自建（hook 不做任何主动扫描）",
                 (unsigned)g_gateInfo.quietMs, (unsigned)g_gateInfo.dbsTracked);
        g_waitChecks = 0;
        SetGateState(buf, 1, 1);
    }
    return 0;
}

void Evt_WeixinLoad()
{
    g_hWeixinDll = GetModuleHandleW(L"Weixin.dll");
    if (!g_hWeixinDll)
    {
        return;
    }

    if (InterlockedCompareExchange(&g_readyThreadStarted, 1, 0) == 0) {
        FILETIME ft{};
        GetSystemTimeAsFileTime(&ft);
        g_loginLoadTime.LowPart  = ft.dwLowDateTime;
        g_loginLoadTime.HighPart = ft.dwHighDateTime;
        CreateThread(nullptr, 0, LoginReadyThread, nullptr, 0, nullptr);
    }

#ifdef _DEBUG
    char debugMsg[256];
    snprintf(debugMsg, sizeof(debugMsg),"[Evt_WeixinLoad] Weixin.dll: 0x%p\n",g_hWeixinDll);
    OutputDebugStringA(debugMsg);
#endif


    进程PID = GetCurrentProcessId();
    父进程PID = GetParentProcessId();

    //过低版本 4.1.8.67
    //Patch_Low_Version_m2();

    //get base DirPath
    //InitStandardPaths();
    
    Patch_Revoke();


    // 创建并启动HTTP服务器
    if (!g_httpServer)
    {
        g_httpServer = new HttpServer();
        g_httpServer->Start("0.0.0.0", g_StartPort);
    }
    // xLog 明文捕获：**默认关**（只有 %TEMP%\wx_xlog_capture.on 存在才装）。
    // 为什么需要：盘上的 .xlog 是 RSA 加密的，唯一能拿到明文的地方就是
    // mars xlog 写入路径、加密之前。旧代码用的 0xF22C1 偏移对本版已过期
    // （实测不是指令边界），所以由 InstallXlogCapture 自己按 RVA 装。
    hook::InstallXlogCapture();

    // 语音通话邀请对象的静默抓取：**同样默认关**（只有 %TEMP%\wx_voip_capture.on
    // 存在才装）。为什么要有它：探针版 version.dll 装了 19 个钩子、每命中一次
    // 同步写 3KB、还挂了 ws2_32!send，实测一发消息就把微信卡死（重启 4 次）。
    // 这一版**只钩一个点**（Weixin!0x2319D00，voipinvitemsg 消息层），
    // 命中上限 24 次、超了一个字节都不写 —— 只为"打一次真电话、把邀请对象的
    // 字段布局干净地抓回来"。详见 _audit/通话功能-逆向进度与恢复.md 第十轮。
    hook::InstallVoipCapture();
    

    
     

    
    //取回调URL
    GetWxRecvUrl();         
}

