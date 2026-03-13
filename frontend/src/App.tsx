import { useGpu } from './hooks/useGpu';
import { useCurve } from './hooks/useCurve';
import { useMonitor } from './hooks/useMonitor';
import { StatusBar } from './components/Monitor/StatusBar';
import { LiveMonitor } from './components/Monitor/LiveMonitor';
import { CurveEditor } from './components/CurveEditor/CurveEditor';
import { PointTable } from './components/PointTable/PointTable';
import { PerformancePanel } from './components/Limits/PerformancePanel';
import { PerformanceMonitor } from './components/Monitor/PerformanceMonitor';
import { ProfilePanel } from './components/Profiles/ProfilePanel';
import { api } from './api/client';
import { useCurveStore } from './store/curveStore';
import { Toaster } from 'sonner';
import { Loader, ChevronDown } from 'lucide-react';
import { useState, useRef, useEffect } from 'react';

export default function App() {
  const gpuInfo = useGpu();
  const { curve, wsStatus: curveWsStatus } = useCurve();
  const { monitor, monitorHistory, wsStatus: monitorWsStatus } = useMonitor();
  const { setCurve, activeProfile, setActiveProfile } = useCurveStore();

  const [activeTab, setActiveTab] = useState<'curve' | 'performance'>('curve');
  const [activeDomain, setActiveDomain] = useState<'gpu' | 'memory'>('gpu');
  const [isProfileOpen, setIsProfileOpen] = useState(false);
  const profileRef = useRef<HTMLDivElement>(null);

  const currentVoltageMv = monitor?.voltage_mv ?? null;

  function handleRefresh() {
    api.curve().then(setCurve).catch(console.error);
  }

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (profileRef.current && !profileRef.current.contains(event.target as Node)) {
        setIsProfileOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);

    // Fetch initial active profile
    api.profiles().then(data => setActiveProfile(data.active)).catch(console.error);

    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, []);

  // Worst connection status wins
  const wsStatus =
    monitorWsStatus === 'disconnected' || curveWsStatus === 'disconnected'
      ? 'disconnected'
      : monitorWsStatus === 'connecting' || curveWsStatus === 'connecting'
        ? 'connecting'
        : 'connected';

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100 flex flex-col">
      <Toaster theme="dark" position="top-right" />
      <StatusBar gpuInfo={gpuInfo} wsStatus={wsStatus} monitor={monitor} />

      {wsStatus !== 'connected' && (
        <div className="bg-amber-500/10 border-b border-amber-500/20 text-amber-500 px-4 py-2 text-center text-sm font-medium flex justify-center items-center gap-2">
          <Loader size={14} className="animate-spin" />
          {wsStatus === 'connecting' ? 'Reconnecting to backend...' : 'Connection lost. Retrying...'}
        </div>
      )}

      <main className="flex-1 p-4 flex flex-col gap-4 w-full max-w-[1600px] mx-auto">
        {/* Tab Header and Profile Selector */}
        <div className="flex justify-between items-end border-b border-zinc-800 pb-2">
          <div className="flex gap-6">
            <button
              onClick={() => setActiveTab('curve')}
              className={`text-lg font-medium pb-2 -mb-[9px] border-b-2 transition-colors ${activeTab === 'curve' ? 'border-pink-500 text-zinc-100' : 'border-transparent text-zinc-500 hover:text-zinc-300'}`}
            >
              Curve
            </button>
            <button
              onClick={() => setActiveTab('performance')}
              className={`text-lg font-medium pb-2 -mb-[9px] border-b-2 transition-colors ${activeTab === 'performance' ? 'border-pink-500 text-zinc-100' : 'border-transparent text-zinc-500 hover:text-zinc-300'}`}
            >
              Performance
            </button>
          </div>
          <div className="relative" ref={profileRef}>
            <button
              onClick={() => setIsProfileOpen(!isProfileOpen)}
              className="flex items-center gap-2 text-zinc-400 hover:text-zinc-100 transition-colors text-sm font-medium pb-2 -mb-[9px]"
            >
              {activeProfile ? (
                <>
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 shrink-0" />
                  <span className="text-zinc-200">{activeProfile}</span>
                </>
              ) : (
                'Profiles'
              )}
              <ChevronDown size={14} className={isProfileOpen ? "rotate-180 transition-transform" : "transition-transform"} />
            </button>
            {isProfileOpen && (
              <div className="absolute right-0 top-full mt-2 w-80 z-50 shadow-[0_8px_30px_rgb(0,0,0,0.8)] border border-zinc-700/50 rounded-lg overflow-hidden">
                <ProfilePanel activeProfile={activeProfile} onProfileApplied={setActiveProfile} />
              </div>
            )}
          </div>
        </div>

        {/* Main content area */}
        <div className="flex flex-col gap-4 w-full">
          {activeTab === 'curve' ? (
            <>
              <div className="flex gap-4 items-stretch w-full">
                <div className="flex-1 min-w-0 flex flex-col">
                  {curve ? (
                    <CurveEditor
                      curve={curve}
                      activeDomain={activeDomain}
                      onDomainChange={setActiveDomain}
                      currentVoltageMv={currentVoltageMv}
                      currentClockMhz={monitor?.clock_mhz ?? null}
                      onRefresh={handleRefresh}
                    />
                  ) : (
                    <div className="bg-zinc-900 rounded-lg p-8 text-center text-zinc-500 h-full flex items-center justify-center">
                      Loading curve…
                    </div>
                  )}
                </div>
                
                <div className="w-80 shrink-0 flex flex-col">
                  <LiveMonitor monitor={monitor} history={monitorHistory} />
                </div>
              </div>
              
              {curve && (
                <div className="w-full">
                  <PointTable
                    points={curve.points.filter(p => p.domain === activeDomain)}
                    currentVoltageMv={activeDomain === 'gpu' ? currentVoltageMv : null}
                    readOnly={activeDomain === 'memory'}
                  />
                </div>
              )}
            </>
          ) : (
            <div className="flex gap-4 items-start w-full">
              <div className="flex-1 min-w-0">
                <PerformancePanel />
              </div>
              <div className="w-80 shrink-0 flex flex-col">
                <PerformanceMonitor monitor={monitor} history={monitorHistory} />
              </div>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
