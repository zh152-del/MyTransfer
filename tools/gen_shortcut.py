# -*- coding: utf-8 -*-
"""生成「接收电脑文件.shortcut」（binary plist）。

多文件版：status 一次拿到 files 数组，快捷指令用「重复每个」循环
下载 → 保存 → 回报 → 通知，队列里几个文件就收几个。

动作（与 /api/shortcut/* 一一对应，全部固定 URL，零手填）：
  1. 获取 URL 内容  http://MyTransfer.local:8765/api/shortcut/status
  2. 获取词典值 hasFile
  3. 如果 hasFile 等于 "true"
  4.   获取词典值 files
  5.   重复每个 files
  6.     获取词典值 fileName ← 重复项
  7.     获取词典值 fileUrl  ← 重复项
  8.     获取 URL 内容（fileUrl）→ 文件本体
  9.     保存文件 → MyTransfer（覆盖）
 10.     获取词典值 taskId ← 重复项
 11.     获取 URL 内容  .../api/shortcut/complete?taskId=<taskId>
 12.     显示通知「接收完成：<fileName>」
 13.   结束重复
 14. 否则：显示通知「电脑当前没有待接收文件」
 15. 结束如果

诚实声明：文件按 Shortcuts plist 结构生成并自检可解析，但没有真机环境，
**未经 iPhone 实际导入验证**；iOS 15+ 会拦截未签名快捷指令（Apple 策略），
导入失败请按 docs/Shortcut安装说明.md 手动搭建。
"""
import os
import plistlib
import uuid

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shortcut")
NAME = "接收电脑文件"
BASE = "http://MyTransfer.local:8765"


def act(identifier, params):
    params = dict(params)
    params.setdefault("UUID", uuid.uuid4().hex)
    return {"WFWorkflowActionIdentifier": identifier,
            "WFWorkflowActionParameters": params}


def text(value, attachments=None):
    if not attachments:
        return value
    return {
        "Value": {
            "attachments": {k: {"Type": "ActionOutput", "OutputUUID": k,
                                "OutputName": v}
                            for k, v in attachments.items()},
            "string": value,
        },
        "WFSerializationType": "WFTextTokenString",
    }


def var(action_uuid, name):
    return {"Value": {"Type": "ActionOutput", "OutputUUID": action_uuid,
                      "OutputName": name},
            "WFSerializationType": "WFTextTokenAttachment"}


def build_actions():
    a = []
    # 1. status
    s1 = act("is.workflow.actions.geturlcontent",
             {"WFURL": BASE + "/api/shortcut/status",
              "ShowHeaders": False, "WFHTTPMethod": "GET"})
    a.append(s1)
    u1 = s1["WFWorkflowActionParameters"]["UUID"]
    # 2. hasFile
    s2 = act("is.workflow.actions.getvalueforkey",
             {"WFGetDictionaryValueKey": "hasFile",
              "WFInput": var(u1, "hasFile")})
    a.append(s2)
    u2 = s2["WFWorkflowActionParameters"]["UUID"]
    # 3. 如果 hasFile == "true"
    gid = uuid.uuid4().hex
    a.append(act("is.workflow.actions.conditional",
                 {"GroupingIdentifier": gid, "WFCondition": 4,
                  "WFConditionalActionString": "true",
                  "WFInput": var(u2, "hasFile")}))
    # 4. files
    s4 = act("is.workflow.actions.getvalueforkey",
             {"WFGetDictionaryValueKey": "files",
              "WFInput": var(u1, "files")})
    a.append(s4)
    u4 = s4["WFWorkflowActionParameters"]["UUID"]
    # 5. 重复每个 files
    gid_rep = uuid.uuid4().hex
    rep = act("is.workflow.actions.repeat.each",
              {"WFInput": var(u4, "files"),
               "GroupingIdentifier": gid_rep})
    a.append(rep)
    u_rep = rep["WFWorkflowActionParameters"]["UUID"]

    def from_item(key):
        return act("is.workflow.actions.getvalueforkey",
                   {"WFGetDictionaryValueKey": key,
                    "WFInput": var(u_rep, "Repeat Item")})

    # 6. fileName
    s6 = from_item("fileName")
    a.append(s6)
    u6 = s6["WFWorkflowActionParameters"]["UUID"]
    # 7. fileUrl
    s7 = from_item("fileUrl")
    a.append(s7)
    u7 = s7["WFWorkflowActionParameters"]["UUID"]
    # 8. 下载
    s8 = act("is.workflow.actions.geturlcontent",
             {"WFURL": text("", {u7: "fileUrl"}),
              "ShowHeaders": False, "WFHTTPMethod": "GET"})
    a.append(s8)
    u8 = s8["WFWorkflowActionParameters"]["UUID"]
    # 9. 保存
    a.append(act("is.workflow.actions.documentpicker.save",
                 {"WFAskWhereToSave": False,
                  "WFFileDestinationPath": "/MyTransfer",
                  "WFOverwrite": True,
                  "WFInput": var(u8, "文件")}))
    # 10. taskId
    s10 = from_item("taskId")
    a.append(s10)
    u10 = s10["WFWorkflowActionParameters"]["UUID"]
    # 11. 回报完成（taskId 拼进网址）
    a.append(act("is.workflow.actions.geturlcontent",
                 {"WFURL": text(BASE + "/api/shortcut/complete?taskId=",
                                {u10: "taskId"}),
                  "ShowHeaders": False, "WFHTTPMethod": "GET"}))
    # 12. 通知
    a.append(act("is.workflow.actions.notification",
                 {"WFNotificationActionBody":
                  text("接收完成，已保存到「文件 → 我的 iPhone → MyTransfer」",
                       {u6: "fileName"})}))
    # 13. 结束重复
    a.append(act("is.workflow.actions.repeat.each",
                 {"GroupingIdentifier": gid_rep}))
    # 14. 否则
    a.append(act("is.workflow.actions.conditional",
                 {"GroupingIdentifier": gid}))
    a.append(act("is.workflow.actions.notification",
                 {"WFNotificationActionBody":
                  "电脑当前没有待接收文件\n\n请确认：\n1. 电脑程序正在运行\n2. 手机和电脑连的是同一个 Wi-Fi"}))
    # 15. 结束如果
    a.append(act("is.workflow.actions.endif",
                 {"GroupingIdentifier": gid}))
    return a


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    doc = {
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowClientVersion": "2038.0.2.4",
        "WFWorkflowName": NAME,
        "WFWorkflowIcon": {
            "WFWorkflowIconStartColor": 4282601983,
            "WFWorkflowIconGlyphNumber": 59511,
        },
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": ["NCWidget", "WatchKit"],
        "WFWorkflowInputContentItemClasses": [
            "WFAppStoreAppContentItem", "WFArticleContentItem",
            "WFContactContentItem", "WFDateContentItem",
            "WFEmailAddressContentItem", "WFFolderContentItem",
            "WFGenericFileContentItem", "WFImageContentItem",
            "WFiTunesProductContentItem", "WFLocationContentItem",
            "WFDCMapsLinkContentItem", "WFAVAssetContentItem",
            "WFPDFContentItem", "WFPhoneNumberContentItem",
            "WFRichTextContentItem", "WFSafariWebPageContentItem",
            "WFStringContentItem", "WFURLContentItem",
        ],
        "WFWorkflowActions": build_actions(),
    }
    out = os.path.join(OUT_DIR, NAME + ".shortcut")
    with open(out, "wb") as f:
        plistlib.dump(doc, f, fmt=plistlib.FMT_BINARY)
    with open(out, "rb") as f:
        back = plistlib.load(f)
    n = len(back["WFWorkflowActions"])
    print("生成：%s" % out)
    print("自检：plist 可解析，%d 个动作，名称=%s" % (n, back["WFWorkflowName"]))
    ids = [x["WFWorkflowActionIdentifier"] for x in back["WFWorkflowActions"]]
    for i in ids:
        print("  -", i.replace("is.workflow.actions.", ""))


if __name__ == "__main__":
    main()
