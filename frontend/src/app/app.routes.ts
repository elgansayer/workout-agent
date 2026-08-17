import { Routes } from '@angular/router';
import { authGuard } from './services/auth/auth-guard';

import { UserProfile } from './components/user-profile/user-profile';
import { Dashboard } from './components/dashboard/dashboard';
import { Chat } from './components/chat/chat';
import { Checkins } from './components/checkins/checkins';
import { History } from './components/history/history';
import { Login } from './components/login/login';
import { Plan } from './components/plan/plan';
import { Programmes } from './components/programmes/programmes';
import { Progress } from './components/progress/progress';
import { Settings } from './components/settings/settings';
import { Stats } from './components/stats/stats';

export const routes: Routes = [
  { path: 'login', component: Login },
  { path: 'profile', component: UserProfile, canActivate: [authGuard] },
  { path: 'dashboard', component: Dashboard, canActivate: [authGuard] },
  { path: 'chat', component: Chat, canActivate: [authGuard] },
  { path: 'checkins', component: Checkins, canActivate: [authGuard] },
  { path: 'history', component: History, canActivate: [authGuard] },
  { path: 'plan', component: Plan, canActivate: [authGuard] },
  { path: 'programmes', component: Programmes, canActivate: [authGuard] },
  { path: 'progress', component: Progress, canActivate: [authGuard] },
  { path: 'settings', component: Settings, canActivate: [authGuard] },
  { path: 'stats', component: Stats, canActivate: [authGuard] },
  { path: '', redirectTo: '/dashboard', pathMatch: 'full' },
  { path: '**', redirectTo: '/dashboard' }
];
