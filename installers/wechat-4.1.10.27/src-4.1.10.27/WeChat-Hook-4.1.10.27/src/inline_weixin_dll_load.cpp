#include <cstdint>
#include <cstdio>
#include <string>
#include <Windows.h>
#include <winternl.h>
#include <cstring>
#include <sstream>
#include <atomic>
#include <cctype>
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


// ===== 本地补丁：等「登录就绪」再放行 =====
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
// 改成等一个可信的就绪信号：微信登录后会往
//   %USERPROFILE%\Documents\xwechat_files\<wxid>\db_storage\*\*.db
// 写数据。发现有 .db 的修改时间晚于「本 DLL 加载时刻」，就说明这次进程已经
// 登录完成、DB 层在活动。等不到就一直是 0，查询返回空，但**不会崩**。

static ULARGE_INTEGER g_loadTime{};
static volatile LONG  g_readyThreadStarted = 0;

// 注意：FindFirstFileW **不支持路径中间的 `*`**（会直接返回 ERROR_INVALID_NAME=123），
// 所以只能一层层往下枚举，通配符永远放在最后一段。
static bool IsDotDir(const wchar_t* name)
{
    return name[0] == L'.' &&
           (name[1] == 0 || (name[1] == L'.' && name[2] == 0));
}

static bool DirHasFreshDb(const std::wstring& dir)
{
    WIN32_FIND_DATAW fd = {};
    HANDLE h = FindFirstFileW((dir + L"\\*.db").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE)
        return false;

    bool ok = false;
    do {
        ULARGE_INTEGER w{};
        w.LowPart  = fd.ftLastWriteTime.dwLowDateTime;
        w.HighPart = fd.ftLastWriteTime.dwHighDateTime;
        // 留 5 秒时钟误差
        if (w.QuadPart != 0 && w.QuadPart + 5ULL * 10000000ULL >= g_loadTime.QuadPart) {
            ok = true;
            break;
        }
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return ok;
}

// ---- 「登录就绪」的候选数据根 ----
//
// ⚠️ 和 Python 侧 `image_cache._wechat_save_roots()` 是**同一个判据，两处必须一致**：
// 微信 4.x 把「文件保存位置」记在 %APPDATA%\Tencent\xwechat\config\<哈希>.ini，
// 文件内容就是**一行路径**（本机实测：`D:\wechat`）。
//
// 为什么必须有这条（2026-10-03 真机踩到）：以前这里把路径**写死**成
// `%USERPROFILE%\Documents\xwechat_files`。用户 2026-10-02 22:25 把微信数据搬到
// `D:\wechat` 之后，那条路径**不存在**了 → 下面这个就绪判据永远不成立 →
// `g_IsLogin` 恒为 0 → `db_mgr` 的门禁永远关着 → 所有 QueryDB 一律回
// 「get database handle which named xxx.db failed」。**不报错、不打日志**，
// 正是这个项目最怕的那种静默失效。Python 侧当年踩的是同一个坑、那边已经修了，
// 这边没同步 —— 这是**重复 owner** 的代价。
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

// 微信自己记的保存位置（xwechat_files 的**父目录**）。判据见上面那段注释。
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

// 登录就绪判据的**可读诊断**，发布给 /QueryDB/status。
// 只写一次（写侧只有登录线程）、读侧拿原子快照 —— 不用锁。
static std::string g_gateNoteBuf;
static std::atomic<const char*> g_gateNotePtr{""};

void SetLoginGateNote(const std::string& s)
{
    if (g_gateNotePtr.load()[0] != '\0')
        return;
    g_gateNoteBuf = s;
    g_gateNotePtr.store(g_gateNoteBuf.c_str());
}

const char* LoginGateNote()
{
    return g_gateNotePtr.load();
}

// 在某个数据根下找「本 DLL 加载之后被写过的 .db」
static bool RootHasFreshDb(const std::wstring& root)
{
    WIN32_FIND_DATAW fd = {};
    HANDLE h = FindFirstFileW((root + L"\\*").c_str(), &fd);
    if (h == INVALID_HANDLE_VALUE)
        return false;

    bool ok = false;
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
            if (DirHasFreshDb(storage + L"\\" + sd.cFileName)) {
                ok = true;
                break;
            }
        } while (FindNextFileW(sh, &sd));
        FindClose(sh);
        if (ok)
            break;
    } while (FindNextFileW(h, &fd));
    FindClose(h);
    return ok;
}

static bool WxDbWrittenSinceLoad()
{
    // 候选按优先级：① 微信自己记的保存位置 ② 历史默认位置。
    std::vector<std::wstring> roots;
    for (const std::wstring& r : WechatSaveRoots())
        roots.push_back(r + L"\\xwechat_files");

    wchar_t profile[MAX_PATH] = {};
    if (GetEnvironmentVariableW(L"USERPROFILE", profile, MAX_PATH))
        roots.push_back(std::wstring(profile) + L"\\Documents\\xwechat_files");

    int existing = 0;
    for (const std::wstring& root : roots) {
        if (GetFileAttributesW(root.c_str()) == INVALID_FILE_ATTRIBUTES)
            continue;
        existing++;
        if (RootHasFreshDb(root))
            return true;
    }

    if (existing == 0) {
        // 一个候选根都不存在 = **配置问题**（数据被搬到别处了），不是「还没登录」。
        // 如实写进 /QueryDB/status，别让用户对着一个恒为 0 的 IsLogin 猜。
        std::string note = "登录就绪判据失败：候选数据根一个都不存在 -> ";
        for (size_t i = 0; i < roots.size(); i++) {
            if (i)
                note += " | ";
            note += Utf8FromWide(roots[i]);
        }
        SetLoginGateNote(note);
    }
    return false;
}

static DWORD WINAPI LoginReadyThread(LPVOID)
{
    for (int i = 0; i < 3600; i++) {          // 最多等 30 分钟
        if (WxDbWrittenSinceLoad()) {
            Sleep(3000);                      // 登录瞬间句柄和索引还在建，缓一缓
            if (WxDbWrittenSinceLoad()) {
                g_IsLogin = 1;                // 放行 db_mgr 门禁
                SetLoginGateNote("ok：已在微信自己记的保存位置下看到新写入的 db");
                return 0;
            }
        }
        Sleep(500);
    }
    // 30 分钟都没等到 —— 把结论写出去，别再让用户对着恒为 0 的 IsLogin 猜。
    {
        std::string note = "登录就绪判据 30 分钟未命中（不是崩溃，是判据没找到数据）。已试过：";
        wchar_t profile[MAX_PATH] = {};
        GetEnvironmentVariableW(L"USERPROFILE", profile, MAX_PATH);
        const std::wstring fallback =
            std::wstring(profile) + L"\\Documents\\xwechat_files";
        bool any = false;
        for (const std::wstring& r : WechatSaveRoots()) {
            note += Utf8FromWide(r + L"\\xwechat_files") + " ";
            any = true;
        }
        if (!any)
            note += "（微信的 config\\*.ini 里没读到任何有效保存位置）";
        note += " | 默认位置 " + Utf8FromWide(fallback);
        SetLoginGateNote(note);
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
        g_loadTime.LowPart  = ft.dwLowDateTime;
        g_loadTime.HighPart = ft.dwHighDateTime;
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

