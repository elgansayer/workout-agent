import { Routes } from '@angular/router';
import { authGuard } from './services/auth/auth-guard';

import { UserProfile } from './components/user-profile/user-profile';
import { Dashboard } from './components/dashboard/dashboard';
import { Chat } from './components/chat/chat';
import { Checkins } from './components/checkins/checkins';
import { History } from './components/history/history';
import { Login } from './components/login/login';
import { Recovery } from './components/recovery/recovery';
import { Plan } from './components/plan/plan';
import { Progress } from './components/progress/progress';
import { Settings } from './components/settings/settings';
import { Stats } from './components/stats/stats';

export const routes: Routes = [
  { path: 'login', component: Login, title: 'Sign in · Workout Agent' },
  {
    path: 'recover',
    component: Recovery,
    title: 'Recover your account · Workout Agent',
  },
  {
    path: 'profile',
    component: UserProfile,
    title: 'Account · Workout Agent',
    canActivate: [authGuard],
  },
  {
    path: 'dashboard',
    component: Dashboard,
    title: 'Today · Workout Agent',
    canActivate: [authGuard],
  },
  { path: 'chat', component: Chat, title: 'Coach · Workout Agent', canActivate: [authGuard] },
  {
    path: 'checkins',
    component: Checkins,
    title: 'Check-ins · Workout Agent',
    canActivate: [authGuard],
  },
  {
    path: 'history',
    component: History,
    title: 'History · Workout Agent',
    canActivate: [authGuard],
  },
  { path: 'plan', component: Plan, title: 'Plan · Workout Agent', canActivate: [authGuard] },
  {
    path: 'programmes',
    loadComponent: () => import('./components/programmes/programmes').then((m) => m.Programmes),
    title: 'Programme builder · Workout Agent',
    canActivate: [authGuard],
  },
  {
    path: 'progress',
    component: Progress,
    title: 'Progress · Workout Agent',
    canActivate: [authGuard],
  },
  {
    path: 'onboarding',
    loadComponent: () => import('./components/onboarding/onboarding').then((m) => m.Onboarding),
    title: 'Setup · Workout Agent',
    canActivate: [authGuard],
  },
  {
    path: 'settings',
    component: Settings,
    title: 'Settings · Workout Agent',
    canActivate: [authGuard],
  },
  { path: 'stats', component: Stats, title: 'Stats · Workout Agent', canActivate: [authGuard] },
  { path: '', redirectTo: '/dashboard', pathMatch: 'full' },
  { path: '**', redirectTo: '/dashboard' },
];
