import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { RegisterClient } from "./register-client";

export const metadata: Metadata = {
  title: "Sign up",
};

export default function RegisterPage() {
  if (process.env.LOCAL_DEV_AUTO_LOGIN === "1") {
    redirect("/login");
  }
  return <RegisterClient />;
}

export const dynamic = "force-dynamic";
