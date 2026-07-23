import type { ReactNode } from "react";
import { Activity, FolderOpen, ImageIcon, MessageSquare, Play, Server, Terminal, Users } from "lucide-react";

export type ActiveView =
  | "overview"
  | "register"
  | "accounts"
  | "cliTest"
  | "chatTest"
  | "imageTest"
  | "relay"
  | "logs";

export interface AppRoute {
  id: ActiveView;
  path: string;
  label: string;
  title: string;
  subtitle: string;
  icon: ReactNode;
}

export const appRoutes: AppRoute[] = [
  {
    id: "overview",
    path: "/",
    label: "总览",
    title: "系统总览",
    subtitle: "任务状态、账号资产和本地中转概览",
    icon: <Activity size={17} />,
  },
  {
    id: "register",
    path: "/register",
    label: "注册任务",
    title: "注册任务",
    subtitle: "创建账号、控制并发和查看任务进度",
    icon: <Play size={17} />,
  },
  {
    id: "accounts",
    path: "/accounts",
    label: "账号列表",
    title: "账号列表",
    subtitle: "查看、选择、导出和刷新本地凭据",
    icon: <Users size={17} />,
  },
  {
    id: "cliTest",
    path: "/grok-cli-test",
    label: "Grok CLI测试",
    title: "Grok CLI 测试",
    subtitle: "选择账号池账号，验证 grok-4.5 CLI 能力并导出可用账号",
    icon: <Terminal size={17} />,
  },
  {
    id: "chatTest",
    path: "/chat-test",
    label: "Chat 对话",
    title: "Chat 对话测试",
    subtitle: "验证 CLI 4.5 对话，4.20/4.3 模型走 grok2api Chat Completions",
    icon: <MessageSquare size={17} />,
  },
  {
    id: "imageTest",
    path: "/image-test",
    label: "图片生成",
    title: "图片生成测试",
    subtitle: "默认使用 basic SSO 池的 Imagine Lite，标准/pro 无可用池时自动降级",
    icon: <ImageIcon size={17} />,
  },
  {
    id: "relay",
    path: "/relay",
    label: "本地中转",
    title: "统一 API 入口",
    subtitle: "前端和 OpenAI 兼容接口共用当前后端端口",
    icon: <Server size={17} />,
  },
  {
    id: "logs",
    path: "/logs",
    label: "任务日志",
    title: "任务日志",
    subtitle: "查看最近任务事件和失败信息",
    icon: <FolderOpen size={17} />,
  },
];

export function routeForPath(pathname: string): AppRoute {
  const normalized = pathname.replace(/\/+$/, "") || "/";
  return appRoutes.find((route) => route.path === normalized) || appRoutes[0];
}

export function routeForView(view: ActiveView): AppRoute {
  return appRoutes.find((route) => route.id === view) || appRoutes[0];
}
