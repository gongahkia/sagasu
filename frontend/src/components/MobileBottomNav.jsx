import { Home, Calendar, CheckSquare } from 'lucide-react';

export default function MobileBottomNav({ activeTab, onTabChange }) {
  const tabs = [
    { id: 'rooms', label: 'Available Rooms', icon: Home },
    { id: 'bookings', label: 'My Bookings', icon: Calendar },
    { id: 'tasks', label: 'My Tasks', icon: CheckSquare }
  ];

  return (
    <nav className="md:hidden fixed bottom-0 left-0 right-0 bg-base-0 border-t border-base-3 z-50">
      <div className="flex justify-around items-center h-16">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;

          return (
            <button
              key={tab.id}
              onClick={() => onTabChange(tab.id)}
              className={`flex flex-col items-center justify-center flex-1 h-full transition-colors ${
                isActive
                  ? 'text-violet-0'
                  : 'text-base-6 hover:text-base-8'
              }`}
              aria-label={tab.label}
              aria-current={isActive ? 'page' : undefined}
            >
              <Icon
                size={24}
                strokeWidth={isActive ? 2.5 : 2}
              />
            </button>
          );
        })}
      </div>
    </nav>
  );
}
