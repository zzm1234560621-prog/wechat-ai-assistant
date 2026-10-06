#pragma once
#include <windows.h>
#include <string>
#include <cstdint>
#include <utility>
#include <vector>

// 前向声明，不需要完整类型
class HttpServer;

extern int g_receive_type;
extern int g_StartPort;
extern int g_MsgSendPort;
extern HMODULE g_hWeixinDll;
extern HMODULE g_hWeixinExe;


extern HANDLE g_hLoginMonitor;
extern HANDLE g_hAfterLoginInit;

extern DWORD g_MainThreadId;
extern DWORD 进程PID;
extern DWORD 父进程PID;
extern HWND  g_WeixinMainHwnd;
extern volatile uint64_t g_IsLogin;   // 0=未登录 1=已登录
// db 层是否「真能用」——由就绪判据线程在**验过句柄可查**之后才置 1（2026-10-05 加）。
// 与 g_IsLogin 的分工：g_IsLogin 是对外可见的状态（IsLogin 字段），g_DbLayerReady 是
// db_mgr 真正消费的门禁。目前两者由同一处一起置位/复位；分开是为了让「对外状态」与
// 「内部门禁」各有一个具名 owner，别再靠 `g_IsLogin` 一个变量同时兼任两件事。
extern volatile uint64_t g_DbLayerReady;
// **句柄表扫描许可**（2026-10-05 修循环依赖时加）：
// 就绪判据要靠「句柄表扫描」来判断 db 层能不能用，而扫描本身又被 g_DbLayerReady 挡着
// —— 那是循环依赖（实测 handles=0/total=0，闸门永远不开）。所以把两件事拆开：
//   g_DbLayerReady   = 查询门禁（放行后才置 1，且会置回 0）
//   g_HandleScanAllowed = 允许 searchDatabases() 真的去扫（判据线程起来后就置 1，常驻）
// 扫描是只读的（enumerate ctx / errcode / PRAGMA database_list），在登录未就绪时扫也不会
// 动微信的东西；这正是旧判据（无条件置 g_IsLogin=1）能拿到句柄的原因。
extern volatile uint64_t g_HandleScanAllowed;
extern volatile uint64_t g_getprofile;

// db 层门禁（唯一读点语义：true = 可以拿句柄查库）。实现在 db_mgr.cpp。
bool DbLayerReady();
// 句柄表存活校验：缓存里还有没有一个「地址能读」的句柄。给就绪判据/看门狗用。
// 实现在 db_mgr.cpp（只有那里能碰 sqlite3 的私有类型，见 inline_weixin_dll_load.cpp 注释）。
bool DbHandleTableAlive();
// 取一次「库名 -> 句柄数值」快照（只读，不重建缓存）。
// 为什么用值而不是 map<string, LPVOID>：避免让调用方 include sqlite3/私有类型。
void DbHandlesSnapshot(std::vector<std::pair<std::string, uint64_t>>& out);
// 强制重扫句柄表（清缓存 + 全内存扫描）。**重活**，只给就绪判据的冷路径用。
void DbForceRescan();

// ---- 就绪判据的可观测状态（2026-10-05 加）----
// 为什么要有它：判据收紧之后，「没开闸门」和「判据跑不起来」在 /QueryDB/status 上都表现为
// 一个空的 LoginGate —— 那正是本项目最怕的静默失效。把每一轮的**真实计数**摆出来，
// 下次就不用靠猜是「文件侧没到阈值」还是「句柄拿不到」还是「线程压根没跑」。
struct LoginGateInfo {
    uint64_t dbReady;        // 1 = 门禁已放行
    uint32_t running;        // 1 = 判据线程在跑（能区分「卡住」和「没起」）
    uint64_t cycles;         // 探测循环跑过多少轮
    uint32_t stableChecks;   // 连续几次看到核心库在写（阈值 3）
    uint32_t groupsFresh;    // 这一轮有几类核心库在写（阈值 2）
    uint32_t handleGroups;   // 上一轮句柄侧拿到几类（阈值 2）；0xFFFFFFFF = 还没扫过
    uint32_t handleTotal;    // 上一轮句柄总数
    uint32_t waitChecks;     // 已经等了几个探测周期
    uint32_t quietMs;        // 距最近一次「有库被写」过了多久（≥30000 才算稳定）
    uint32_t dbsTracked;     // 已记录 mtime 的库数
    uint64_t handlesAlive;   // 缓存里能读的句柄数（看门狗用）
    std::string handleNames; // 句柄表里的库名（诊断用，前 24 个）
};
LoginGateInfo GetLoginGateInfo();


extern volatile bool g_LoginMonitorRunning;
extern std::string g_CallBack_Url;
extern CRITICAL_SECTION g_dbMgrCriticalSection;

extern std::wstring g_AppDataDir;
extern std::wstring g_DocumentDir;
extern std::wstring g_UsersDir;
extern HMODULE g_hModule;

extern uint64_t g_MyModuleBase;
extern uint64_t g_MyModuleSize;
extern uint64_t g_MyModuleEnd;

// 「登录就绪」判据的诊断文本（空串 = 正常/还没结论）。
// 由 QueryDB.cpp 的 /QueryDB/status 透出去：IsLogin 恒为 0 时，用户能一眼看到原因。
// 见 inline_weixin_dll_load.cpp 里 WxDbWrittenSinceLoad() 的注释。
const char* LoginGateNote();


struct SelfInfo_t
{
    std::string wxid;
    std::string alias;
    std::string nickname;
    std::string phone;
    std::string email;

    uint64_t qq;
    std::string proiv;
    std::string area;
    std::string signinfo;


};
// 全局唯一实例
extern SelfInfo_t SelfInfo;

#define WX_ADDR(offset) ((void*)((uintptr_t)g_hWeixinDll + (offset)))
#define XWECHAT_MAIN_CLAZZ_OFFSET            ((void*)((uintptr_t)g_hWeixinDll + 0xA83AB20))

inline std::wstring g_MyDir;

inline HttpServer* g_httpServer = nullptr;

inline constexpr uint64_t g_Patch_Revoke = 0x22D09E7;


constexpr size_t XWECHAT_SQLITE3_VFS_OFFSET = 0xA6C0490;
constexpr size_t XWECHAT_SQLITE3_API_ROUTINES_OFFSET = 0x8BB0D38;
constexpr size_t XWECHAT_SQLCIPHER_API_ROUTINES_OFFSET = 0x8BB1570;
constexpr size_t XWECHAT_SQLITE3_CODEC_GET_KEY_FUNC = 0x4EE64D0;	//可以废弃不用

namespace offset
{
    inline constexpr uint64_t dec_pic_call = 0x493E70;
    inline constexpr uint64_t create_param2 = 0xDF40;
    inline constexpr uint64_t send_message = 0x1677A30;
    inline constexpr uint64_t param1_vtable = 0x84EC9C8; 

    inline constexpr uint64_t param2 = 0xA0CE0B0;
    inline constexpr uint64_t param2_1 = 0x8595F58;
    inline constexpr uint64_t param2_2 = 0x8595E98; 
    inline constexpr uint64_t param2_3 = 0x8595DD8; 
    inline constexpr uintptr_t txt_message_ctr = 0x6B2C30; 
    inline constexpr uintptr_t txt_message_vtbl = 0x8279358;
    inline constexpr uint64_t img_msg_vtbl = 0x84F96B8; 
    inline constexpr uint64_t img_msg_vtb2 = 0x84F9748;

    // ---- 语音通话邀请用到的两个**微信自己的原语**（2026-10-03 逆向，见 _audit/通话档案）----
    // `msg_ctor(this)`：通用消息对象构造器，**写虚表 base+0x81D2458**。
    //   微信自己在 0x173D080 / 0x3481720 里就是 `mov r8d,0x2d8` 开一块缓冲再调它 ——
    //   **对象大小 0x2D8**，而真机打电话时抓到的那个消息对象（H0 的 rdx）
    //   虚表恰好是 0x81D2458、`+0x038` 恰好是对方 wxid —— 两边对得上。
    // `type_dispatch(obj, type)`：按类型做下一步（`cmp dword ptr [rcx+0xc], edx`）。
    inline constexpr uintptr_t msg_ctor = 0xA04560;
    inline constexpr uintptr_t type_dispatch = 0xA1B1B0;
}


//4.1.5.30  xml
namespace Offsets
{
    inline constexpr uintptr_t IMAGE_FIELD_VTABLE = 0x80D1098;          //ok  41930 ? 41923
    inline constexpr uintptr_t IMAGE_FIELD_VTABLE2 = 0x80D1128;         //ok
    inline constexpr uintptr_t IMAGE_DATA_VTABLE = 0x7415D28;
    inline constexpr uintptr_t IMAGE_DATA_VTABLE2 = 0x7415DB8;
    inline constexpr uintptr_t VIDEO_FIELD_VTABLE = 0x750C7F8;
    inline constexpr uintptr_t VIDEO_FIELD_VTABLE2 = 0x750C888;
    inline constexpr uintptr_t ANIMATION_FIELD_VTABLE = 0x750CC18;
    inline constexpr uintptr_t ANIMATION_FIELD_VTABLE2 = 0x750CCA8;
    inline constexpr uintptr_t MESSAGE_STRUCT_VTABLE = 0x76BD388;
    inline constexpr uintptr_t MESSAGE_STRUCT_VTABLE2 = 0x76BD338;
    inline constexpr uintptr_t MESSAGE_PARAM_VTABLE = 0x76BCF38;
    inline constexpr uintptr_t FORWARD_XML_CALL = 0x1CF3D20;
}



