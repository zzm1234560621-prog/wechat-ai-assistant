#pragma once

// 语音通话邀请对象的**静默抓取**（只钩一个点）。
//
// 为什么要它：要把 /CallVoip 做成能用的功能，缺的不是"怎么发"（仓库的
// WeixinSend::SendText 已经把"构造消息 → send_message"这条路走通了），
// 而是**邀请负载里那四个字符串字段到底放什么**。这个只能从一次真实通话里
// 干净地抓出来。
//
// 而线上那个"探针版" version.dll 抓不了：它装了 19 个钩子、每命中一次同步写
// 3KB、还挂了 ws2_32!send —— 实测一发消息就把微信卡死（重启 4 次，每次重扫码）。
//
// 所以这个版本只有三条纪律：
//   1. **只钩一个点**（Weixin!0x2319D00，voipinvitemsg 消息层那个虚方法）；
//   2. **不逐次落盘**：上限 MAX_DUMPS 次，之后一个字节都不写；
//   3. **默认关**：只有 %TEMP%\wx_voip_capture.on 存在才装。
//
// 详见 _audit/通话功能-逆向进度与恢复.md 第十轮。
namespace hook
{
    // 装了返回 true；开关没开 / 基址拿不到 / 钩子装失败都返回 false（并打一条日志）。
    bool InstallVoipCapture();
}
