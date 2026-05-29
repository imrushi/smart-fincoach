"use client";
import { useState, useEffect } from "react";
import {
  LayoutDashboard, Upload, List, Gauge, RotateCcw, Cog, Zap, LogOut
} from "lucide-react";
import Dashboard from "@/components/Dashboard";
import FileUpload from "@/components/FileUpload";
import TransactionList from "@/components/TransactionList";
import BudgetView from "@/components/BudgetView";
import SubscriptionView from "@/components/SubscriptionView";
import LoginPage from "@/components/LoginPage";
import { api } from "@/lib/api";

const NAV_ITEMS = [
  { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
  { id: "upload", label: "Upload", icon: Upload },
  { id: "transactions", label: "Transactions", icon: List },
  { id: "budgets", label: "Budgets", icon: Gauge },
  { id: "subscriptions", label: "Subscriptions", icon: RotateCcw },
];

export default function Home() {
  const [token, setToken] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [activeTab, setActiveTab] = useState("dashboard");
  const [pipelineRunning, setPipelineRunning] = useState(false);

  useEffect(() => {
    const stored = localStorage.getItem("fc_token");
    setToken(stored);
    setChecking(false);
  }, []);

  const logout = () => {
    localStorage.removeItem("fc_token");
    setToken(null);
  };

  if (checking) return null;
  if (!token) return <LoginPage onLogin={(t) => setToken(t)} />;

  const runPipeline = async () => {
    setPipelineRunning(true);
    try {
      await api.runPipeline();
    } catch { }
    setPipelineRunning(false);
  };

  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="sticky top-0 z-50 backdrop-blur-xl bg-[var(--background)]/80 border-b border-[var(--card-border)]">
        <div className="max-w-7xl mx-auto px-4 h-14 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-brand-600 flex items-center justify-center text-white font-bold text-sm">
              ₹
            </div>
            <span className="font-bold text-lg">FinCoach</span>
          </div>
          <div className="flex items-center gap-2">
          <button
            onClick={runPipeline}
            disabled={pipelineRunning}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-brand-600/20 text-brand-400 text-xs font-medium hover:bg-brand-600/30 disabled:opacity-50 transition-all"
            title="Re-run reconciliation, categorization & alerts"
          >
            <Zap className={`w-3.5 h-3.5 ${pipelineRunning ? "animate-pulse" : ""}`} />
            {pipelineRunning ? "Processing..." : "Run Pipeline"}
          </button>
          <button
            onClick={logout}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 text-xs font-medium hover:bg-red-500/20 transition-all"
            title="Logout"
          >
            <LogOut className="w-3.5 h-3.5" />
          </button>
          </div>
        </div>
      </header>

      <div className="flex-1 flex">
        {/* Sidebar nav */}
        <nav className="hidden md:flex flex-col w-52 p-3 gap-1 border-r border-[var(--card-border)]">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            const active = activeTab === item.id;
            return (
              <button
                key={item.id}
                onClick={() => setActiveTab(item.id)}
                className={`flex items-center gap-2.5 px-3 py-2 rounded-lg text-sm font-medium transition-all ${
                  active
                    ? "bg-brand-600/20 text-brand-400"
                    : "text-[var(--muted)] hover:text-white hover:bg-white/5"
                }`}
              >
                <Icon className="w-4 h-4" />
                {item.label}
              </button>
            );
          })}
        </nav>

        {/* Main content */}
        <main className="flex-1 p-4 md:p-6 max-w-6xl">
          {activeTab === "dashboard" && <Dashboard />}
          {activeTab === "upload" && (
            <div className="max-w-2xl">
              <h2 className="text-lg font-semibold mb-4">Upload Statements</h2>
              <FileUpload onSuccess={() => setActiveTab("transactions")} />
              <div className="mt-6 p-4 rounded-xl bg-[var(--card)] border border-[var(--card-border)]">
                <h3 className="text-sm font-semibold mb-2">📋 Supported Formats</h3>
                <ul className="text-xs text-[var(--muted)] space-y-1.5">
                  <li>• <strong>ICICI Bank:</strong> PDF / CSV / Excel statement download</li>
                  <li>• <strong>HDFC Bank:</strong> PDF / CSV / Excel statement download</li>
                  <li>• <strong>PhonePe:</strong> Transaction history export (CSV/Excel)</li>
                  <li>• <strong>Google Pay:</strong> Transaction history export (CSV/Excel)</li>
                </ul>
              </div>
            </div>
          )}
          {activeTab === "transactions" && <TransactionList />}
          {activeTab === "budgets" && <BudgetView />}
          {activeTab === "subscriptions" && <SubscriptionView />}
        </main>
      </div>

      {/* Mobile bottom nav */}
      <nav className="md:hidden sticky bottom-0 bg-[var(--background)]/90 backdrop-blur-xl border-t border-[var(--card-border)] px-2 py-1.5 flex justify-around">
        {NAV_ITEMS.map((item) => {
          const Icon = item.icon;
          const active = activeTab === item.id;
          return (
            <button
              key={item.id}
              onClick={() => setActiveTab(item.id)}
              className={`flex flex-col items-center gap-0.5 px-2 py-1 rounded-lg text-[10px] transition-all ${
                active ? "text-brand-400" : "text-[var(--muted)]"
              }`}
            >
              <Icon className="w-5 h-5" />
              {item.label}
            </button>
          );
        })}
      </nav>
    </div>
  );
}

